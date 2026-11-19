import os
import hmac
import json
from datetime import datetime, timedelta, timezone
from flask import (Flask, render_template, request, redirect,
                   url_for, session, Response)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

from apoio import (TIPOS, CATEGORIAS, NOME_CATEGORIA, EVENTOS, STATUS,
                   STATUS_ATIVOS, DIAS_SEMANA, para_float, moeda, para_data,
                   fmt_num, processar_imagem, resumo, texto_whatsapp,
                   link_whatsapp, texto_lembrete, detectar_conflitos,
                   gerar_ics)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024  # fotos de até 15 MB

# Banco: usa o PostgreSQL da internet se existir, senão o SQLite local
url_banco = os.environ.get('DATABASE_URL', 'sqlite:///walls.db')
if url_banco.startswith(('postgres://', 'postgresql://')):
    url_banco = 'postgresql+psycopg://' + url_banco.split('://', 1)[1]
app.config['SQLALCHEMY_DATABASE_URI'] = url_banco

# Login por senha
app.secret_key = os.environ.get('SECRET_KEY', 'chave-so-para-testes-locais')
app.permanent_session_lifetime = timedelta(days=30)
SENHA = os.environ.get('SENHA_SISTEMA', 'walls123')

db = SQLAlchemy(app)

app.jinja_env.globals['NOME_CATEGORIA'] = NOME_CATEGORIA
app.jinja_env.filters['moeda'] = moeda
app.jinja_env.filters['data_br'] = lambda d: d.strftime('%d/%m/%Y') if d else ''
app.jinja_env.filters['dia_semana'] = lambda d: DIAS_SEMANA[d.weekday()] if d else ''


def agora():
    # Horário de Brasília (o servidor na internet usa UTC)
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)


# ---------------------------------------------------------------
# Login
# ---------------------------------------------------------------
@app.before_request
def exigir_login():
    if request.endpoint in ('login', 'static'):
        return None
    if not session.get('logado'):
        return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    erro = None
    if request.method == 'POST':
        digitada = request.form.get('senha', '').encode()
        if hmac.compare_digest(digitada, SENHA.encode()):
            session['logado'] = True
            session.permanent = True
            return redirect(url_for('index'))
        erro = 'Senha incorreta.'
    return render_template('login.html', erro=erro)


@app.route('/sair')
def sair():
    session.clear()
    return redirect(url_for('login'))


# ---------------------------------------------------------------
# Tabelas do banco
# ---------------------------------------------------------------
class Cliente(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    telefone = db.Column(db.String(30))
    endereco = db.Column(db.String(200))


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    categoria = db.Column(db.String(30), nullable=False)
    preco = db.Column(db.Float, nullable=False, default=0)
    imagem_v = db.Column(db.Integer, default=0)  # 0 = sem foto
    imagem = db.relationship('ItemImagem', uselist=False,
                             cascade='all, delete-orphan')

    @property
    def tem_imagem(self):
        return (self.imagem_v or 0) > 0


class ItemImagem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('item.id'),
                        nullable=False, unique=True)
    dados = db.Column(db.LargeBinary, nullable=False)


class Config(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    preco_litro = db.Column(db.Float, default=6.0)
    km_por_litro = db.Column(db.Float, default=8.0)
    pedagio_padrao = db.Column(db.Float, default=0.0)
    lucro_percentual = db.Column(db.Float, default=20.0)
    valor_ajudante = db.Column(db.Float, default=0.0)


class Orcamento(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(20))
    cliente_id = db.Column(db.Integer, db.ForeignKey('cliente.id'), nullable=False)
    cliente = db.relationship('Cliente')
    tipo = db.Column(db.String(20), default='Som')
    data = db.Column(db.DateTime, default=agora)
    status = db.Column(db.String(20), default='Em aberto')
    versao = db.Column(db.Integer, default=0)
    fechado = db.Column(db.Integer, default=0)
    # evento
    evento_tipo = db.Column(db.String(40))
    evento_data = db.Column(db.Date)
    dia_todo = db.Column(db.Integer, default=0)
    hora_inicio = db.Column(db.String(5))
    hora_fim = db.Column(db.String(5))
    local_nome = db.Column(db.String(120))
    local_endereco = db.Column(db.String(200))
    cidade = db.Column(db.String(80))
    relembrar_em = db.Column(db.Date)
    # valores
    km = db.Column(db.Float, default=0)
    pedagios = db.Column(db.Float, default=0)
    frete = db.Column(db.Float, default=0)
    custo_combustivel = db.Column(db.Float)
    ajudantes = db.Column(db.Integer, default=0)
    valor_ajudante = db.Column(db.Float)
    desconto = db.Column(db.Float, default=0)
    observacoes = db.Column(db.String(500))
    itens = db.relationship('OrcamentoItem', backref='orcamento',
                            cascade='all, delete-orphan')
    versoes = db.relationship('OrcamentoVersao', backref='orcamento',
                              cascade='all, delete-orphan',
                              order_by='OrcamentoVersao.versao')

    @property
    def subtotal(self):
        return sum(i.subtotal for i in self.itens)

    @property
    def total_ajudantes(self):
        return (self.ajudantes or 0) * (self.valor_ajudante or 0)

    @property
    def total(self):
        soma = (self.subtotal + (self.frete or 0) + self.total_ajudantes
                - (self.desconto or 0))
        return max(soma, 0)

    @property
    def grupos(self):
        # Serviços separados por categoria: [(categoria, lista, subtotal)]
        porcat = {}
        for i in self.itens:
            porcat.setdefault(i.categoria, []).append(i)
        chaves = [c for c in CATEGORIAS if c in porcat]
        chaves += [c for c in porcat if c not in CATEGORIAS]
        return [(c, porcat[c], sum(x.subtotal for x in porcat[c])) for c in chaves]


class OrcamentoItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    orcamento_id = db.Column(db.Integer, db.ForeignKey('orcamento.id'), nullable=False)
    item_id = db.Column(db.Integer)
    categoria = db.Column(db.String(30))
    nome = db.Column(db.String(120), nullable=False)
    preco_unit = db.Column(db.Float, nullable=False)
    quantidade = db.Column(db.Integer, nullable=False)

    @property
    def subtotal(self):
        return self.preco_unit * self.quantidade


class OrcamentoVersao(db.Model):
    # Cópia congelada do orçamento no momento do Fechar e enviar
    id = db.Column(db.Integer, primary_key=True)
    orcamento_id = db.Column(db.Integer, db.ForeignKey('orcamento.id'), nullable=False)
    versao = db.Column(db.Integer, nullable=False)
    fechada_em = db.Column(db.DateTime, default=agora)
    dados = db.Column(db.Text, nullable=False)


# ---------------------------------------------------------------
# Funções de apoio que usam o banco
# ---------------------------------------------------------------
def pegar_config():
    cfg = Config.query.first()
    if not cfg:
        cfg = Config()
        db.session.add(cfg)
        db.session.commit()
    return cfg


def custo_combustivel(cfg, km):
    if not cfg.km_por_litro:
        return 0.0
    return km / cfg.km_por_litro * cfg.preco_litro


def calcular_frete(cfg, km, pedagios):
    base = custo_combustivel(cfg, km) + pedagios
    return round(base * (1 + cfg.lucro_percentual / 100), 2)


def proximo_numero(ano=None):
    # Gera ORC-AAAA-NNN, recomeçando a contagem a cada ano
    ano = ano or agora().year
    prefixo = f'ORC-{ano}-'
    maior = 0
    achados = db.session.query(Orcamento.numero).filter(
        Orcamento.numero.like(prefixo + '%')).all()
    for (n,) in achados:
        try:
            maior = max(maior, int(n.rsplit('-', 1)[1]))
        except (ValueError, IndexError):
            pass
    return f'{prefixo}{maior + 1:03d}'


def lembretes_pendentes():
    hoje = agora().date()
    return (Orcamento.query
            .filter(Orcamento.relembrar_em.isnot(None),
                    Orcamento.relembrar_em <= hoje,
                    Orcamento.status.in_(['Em aberto', 'Enviado']))
            .order_by(Orcamento.relembrar_em).all())


@app.context_processor
def injetar_lembretes():
    n = 0
    if session.get('logado') and request.endpoint not in ('static', 'login'):
        n = len(lembretes_pendentes())
    return {'n_lembretes': n}


NOVAS_COLUNAS = [
    ('config', 'valor_ajudante', 'FLOAT DEFAULT 0'),
    ('item', 'imagem_v', 'INTEGER DEFAULT 0'),
    ('orcamento_item', 'categoria', 'VARCHAR(30)'),
    ('orcamento_item', 'item_id', 'INTEGER'),
    ('orcamento', 'tipo', "VARCHAR(20) DEFAULT 'Som'"),
    ('orcamento', 'numero', 'VARCHAR(20)'),
    ('orcamento', 'status', "VARCHAR(20) DEFAULT 'Em aberto'"),
    ('orcamento', 'versao', 'INTEGER DEFAULT 0'),
    ('orcamento', 'fechado', 'INTEGER DEFAULT 0'),
    ('orcamento', 'evento_tipo', 'VARCHAR(40)'),
    ('orcamento', 'evento_data', 'DATE'),
    ('orcamento', 'dia_todo', 'INTEGER DEFAULT 0'),
    ('orcamento', 'hora_inicio', 'VARCHAR(5)'),
    ('orcamento', 'hora_fim', 'VARCHAR(5)'),
    ('orcamento', 'local_nome', 'VARCHAR(120)'),
    ('orcamento', 'local_endereco', 'VARCHAR(200)'),
    ('orcamento', 'cidade', 'VARCHAR(80)'),
    ('orcamento', 'relembrar_em', 'DATE'),
    ('orcamento', 'custo_combustivel', 'FLOAT'),
    ('orcamento', 'ajudantes', 'INTEGER DEFAULT 0'),
    ('orcamento', 'valor_ajudante', 'FLOAT'),
]


def atualizar_banco():
    # Adiciona colunas novas em bancos criados por versões anteriores
    for tabela, coluna, tipo_sql in NOVAS_COLUNAS:
        colunas = [c['name'] for c in inspect(db.engine).get_columns(tabela)]
        if coluna not in colunas:
            db.session.execute(text(
                f'ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo_sql}'))
            db.session.commit()
    # Numera os orçamentos antigos, na ordem em que foram criados
    sem_numero = (Orcamento.query
                  .filter((Orcamento.numero.is_(None)) | (Orcamento.numero == ''))
                  .order_by(Orcamento.id).all())
    for o in sem_numero:
        o.numero = proximo_numero((o.data or agora()).year)
        db.session.flush()
    # Liga os serviços antigos do orçamento ao cadastro de serviços
    for oi in OrcamentoItem.query.filter(OrcamentoItem.item_id.is_(None)).all():
        it = Item.query.filter_by(nome=oi.nome).first()
        if it:
            oi.item_id = it.id
    db.session.commit()


# ---------------------------------------------------------------
# Início e agenda
# ---------------------------------------------------------------
@app.route('/')
def index():
    total = Orcamento.query.count()
    n_frete = Orcamento.query.filter_by(tipo='Frete').count()
    hoje = agora().date()
    proximos = (Orcamento.query
                .filter(Orcamento.evento_data >= hoje,
                        Orcamento.status.in_(STATUS_ATIVOS))
                .order_by(Orcamento.evento_data, Orcamento.hora_inicio)
                .limit(3).all())
    lembretes = [(o, link_whatsapp(o.cliente.telefone, texto_lembrete(o)))
                 for o in lembretes_pendentes()]
    return render_template(
        'index.html',
        n_equip=total - n_frete,
        n_frete=n_frete,
        total_clientes=Cliente.query.count(),
        total_itens=Item.query.count(),
        proximos=proximos,
        lembretes=lembretes,
    )


@app.route('/agenda')
def agenda():
    filtro = request.args.get('f', 'proximos')
    hoje = agora().date()
    ativos = (Orcamento.query
              .filter(Orcamento.evento_data >= hoje,
                      Orcamento.status.in_(STATUS_ATIVOS)).all())
    conflitos = detectar_conflitos(ativos)
    consulta = Orcamento.query.filter(Orcamento.evento_data.isnot(None))
    if filtro == 'passados':
        consulta = (consulta.filter(Orcamento.evento_data < hoje)
                    .order_by(Orcamento.evento_data.desc(), Orcamento.hora_inicio))
    else:
        consulta = (consulta.filter(Orcamento.evento_data >= hoje)
                    .order_by(Orcamento.evento_data, Orcamento.hora_inicio))
        if filtro == 'aprovados':
            consulta = consulta.filter(Orcamento.status == 'Aprovado')
        else:
            consulta = consulta.filter(Orcamento.status.in_(STATUS_ATIVOS))
    dias = []
    for o in consulta.all():
        if not dias or dias[-1][0] != o.evento_data:
            dias.append((o.evento_data, []))
        dias[-1][1].append(o)
    return render_template('agenda.html', dias=dias, conflitos=conflitos,
                           filtro=filtro, hoje=hoje)


# ---------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------
@app.route('/clientes', methods=['GET', 'POST'])
def clientes():
    if request.method == 'POST':
        novo = Cliente(
            nome=request.form['nome'],
            telefone=request.form.get('telefone'),
            endereco=request.form.get('endereco'),
        )
        db.session.add(novo)
        db.session.commit()
        return redirect(url_for('clientes'))
    lista = Cliente.query.order_by(Cliente.nome).all()
    return render_template('clientes.html', clientes=lista)


@app.route('/clientes/<int:id>/excluir', methods=['POST'])
def excluir_cliente(id):
    db.session.delete(Cliente.query.get_or_404(id))
    db.session.commit()
    return redirect(url_for('clientes'))


# ---------------------------------------------------------------
# Serviços (itens) com foto
# ---------------------------------------------------------------
@app.route('/itens', methods=['GET', 'POST'])
def itens():
    if request.method == 'POST':
        novo = Item(
            nome=request.form['nome'],
            categoria=request.form['categoria'],
            preco=para_float(request.form['preco']),
        )
        db.session.add(novo)
        db.session.flush()
        arq = request.files.get('imagem')
        if arq and arq.filename:
            dados = processar_imagem(arq)
            if dados:
                novo.imagem = ItemImagem(dados=dados)
                novo.imagem_v = 1
        db.session.commit()
        return redirect(url_for('itens'))

    vista = request.args.get('vista')
    if vista in ('lista', 'miniaturas'):
        session['vista'] = vista
    vista = session.get('vista', 'lista')

    cat = request.args.get('cat')
    consulta = Item.query.filter(Item.categoria.in_(CATEGORIAS))
    if cat in CATEGORIAS:
        consulta = consulta.filter_by(categoria=cat)
    else:
        cat = None
    lista = consulta.order_by(Item.categoria, Item.nome).all()
    return render_template('itens.html', itens=lista, categorias=CATEGORIAS,
                           vista=vista, cat=cat)


@app.route('/itens/<int:id>/editar', methods=['GET', 'POST'])
def editar_item(id):
    item = Item.query.get_or_404(id)
    if request.method == 'POST':
        item.nome = request.form['nome']
        item.categoria = request.form['categoria']
        item.preco = para_float(request.form['preco'])
        arq = request.files.get('imagem')
        dados = processar_imagem(arq) if (arq and arq.filename) else None
        if dados:
            if item.imagem:
                item.imagem.dados = dados
            else:
                item.imagem = ItemImagem(dados=dados)
            item.imagem_v = (item.imagem_v or 0) + 1
        elif request.form.get('remover_imagem'):
            item.imagem = None
            item.imagem_v = 0
        db.session.commit()
        return redirect(url_for('itens'))
    return render_template('item_editar.html', item=item, categorias=CATEGORIAS)


@app.route('/itens/<int:id>/imagem')
def imagem_item(id):
    img = ItemImagem.query.filter_by(item_id=id).first_or_404()
    resposta = Response(img.dados, mimetype='image/jpeg')
    resposta.headers['Cache-Control'] = 'private, max-age=31536000'
    return resposta


@app.route('/itens/<int:id>/excluir', methods=['POST'])
def excluir_item(id):
    db.session.delete(Item.query.get_or_404(id))
    db.session.commit()
    return redirect(url_for('itens'))


@app.route('/configuracoes', methods=['GET', 'POST'])
def configuracoes():
    cfg = pegar_config()
    if request.method == 'POST':
        cfg.preco_litro = para_float(request.form['preco_litro'])
        cfg.km_por_litro = para_float(request.form['km_por_litro'], 8.0)
        cfg.pedagio_padrao = para_float(request.form['pedagio_padrao'])
        cfg.lucro_percentual = para_float(request.form['lucro_percentual'])
        cfg.valor_ajudante = para_float(request.form['valor_ajudante'])
        db.session.commit()
        return redirect(url_for('configuracoes'))
    return render_template('configuracoes.html', cfg=cfg)


# ---------------------------------------------------------------
# Orçamentos
# ---------------------------------------------------------------
def aplicar_form(orc, form, cfg):
    # Copia os campos do formulário para o orçamento (novo ou em edição)
    modo = 'frete' if form.get('modo') == 'frete' else 'equip'
    orc.cliente_id = int(form['cliente_id'])
    orc.evento_tipo = form.get('evento_tipo') or None
    orc.evento_data = para_data(form.get('evento_data'))
    orc.dia_todo = 1 if form.get('dia_todo') else 0
    orc.hora_inicio = None if orc.dia_todo else (form.get('hora_inicio') or None)
    orc.hora_fim = None if orc.dia_todo else (form.get('hora_fim') or None)
    orc.local_nome = form.get('local_nome') or None
    orc.local_endereco = form.get('local_endereco') or None
    orc.cidade = form.get('cidade') or None
    orc.observacoes = form.get('observacoes') or None
    orc.desconto = para_float(form.get('desconto'))
    status = form.get('status')
    orc.status = status if status in STATUS else 'Em aberto'
    orc.relembrar_em = para_data(form.get('relembrar_em'))

    antes = orc.ajudantes or 0
    orc.ajudantes = max(int(para_float(form.get('ajudantes'), 0)), 0)
    if orc.ajudantes and (antes == 0 or orc.valor_ajudante is None):
        orc.valor_ajudante = cfg.valor_ajudante or 0

    if modo == 'frete' or form.get('incluir_frete'):
        orc.km = para_float(form.get('km'))
        orc.pedagios = para_float(form.get('pedagios'))
        orc.custo_combustivel = custo_combustivel(cfg, orc.km)
        frete_txt = form.get('frete', '').strip()
        if frete_txt:
            orc.frete = para_float(frete_txt)
        else:
            orc.frete = calcular_frete(cfg, orc.km, orc.pedagios)
    else:
        orc.km = 0
        orc.pedagios = 0
        orc.frete = 0
        orc.custo_combustivel = None

    if modo == 'frete':
        orc.tipo = 'Frete'
        orc.itens = []
        return True

    marcadas = [c for c in CATEGORIAS if c in form.getlist('categorias')]
    if not marcadas:
        return False
    orc.tipo = marcadas[0] if len(marcadas) == 1 else 'Som e iluminação'
    existentes = {i.item_id: i for i in orc.itens if i.item_id}
    novos = []
    for item in Item.query.filter(Item.categoria.in_(marcadas)).all():
        qtd = int(para_float(form.get(f'qtd_{item.id}'), 0))
        if qtd > 0:
            antigo = existentes.get(item.id)
            novos.append(OrcamentoItem(
                item_id=item.id,
                categoria=item.categoria,
                nome=antigo.nome if antigo else item.nome,
                preco_unit=para_float(form.get(f'preco_{item.id}'), 				antigo.preco_unit if antigo else item.preco),
                quantidade=qtd))
    orc.itens = novos
    return True


def valores_form(orc, cfg):
    # Valores que aparecem preenchidos no formulário
    if orc is None:
        return {
            'cliente_id': None, 'evento_tipo': '', 'evento_data': '',
            'dia_todo': False, 'hora_inicio': '', 'hora_fim': '',
            'local_nome': '', 'local_endereco': '', 'cidade': '',
            'incluir_frete': False, 'km': '0',
            'pedagios': fmt_num(cfg.pedagio_padrao), 'frete': '0',
            'ajudantes': 0, 'valor_ajudante': cfg.valor_ajudante or 0,
            'desconto': '0', 'observacoes': '', 'status': 'Em aberto',
            'relembrar_em': '',
        }
    if orc.ajudantes and orc.valor_ajudante is not None:
        valor_aj = orc.valor_ajudante
    else:
        valor_aj = cfg.valor_ajudante or 0
    return {
        'cliente_id': orc.cliente_id,
        'evento_tipo': orc.evento_tipo or '',
        'evento_data': orc.evento_data.isoformat() if orc.evento_data else '',
        'dia_todo': bool(orc.dia_todo),
        'hora_inicio': orc.hora_inicio or '',
        'hora_fim': orc.hora_fim or '',
        'local_nome': orc.local_nome or '',
        'local_endereco': orc.local_endereco or '',
        'cidade': orc.cidade or '',
        'incluir_frete': bool(orc.frete),
        'km': f'{orc.km or 0:g}',
        'pedagios': fmt_num(orc.pedagios),
        'frete': fmt_num(orc.frete),
        'ajudantes': orc.ajudantes or 0,
        'valor_ajudante': valor_aj,
        'desconto': fmt_num(orc.desconto),
        'observacoes': orc.observacoes or '',
        'status': orc.status or 'Em aberto',
        'relembrar_em': orc.relembrar_em.isoformat() if orc.relembrar_em else '',
    }


def mostrar_form(orc, modo, cfg):
    secoes = []
    if modo == 'equip':
        for cat in CATEGORIAS:
            lista = Item.query.filter_by(categoria=cat).order_by(Item.nome).all()
            secoes.append((cat, lista))
    qtds = {i.item_id: i.quantidade for i in orc.itens} if orc else {}
    marcadas = []
    if orc and orc.tipo == 'Som e iluminação':
        marcadas = list(CATEGORIAS)
    elif orc and orc.tipo in CATEGORIAS:
        marcadas = [orc.tipo]
    return render_template(
        'orcamento_form.html',
        orc=orc, modo=modo, cfg=cfg, secoes=secoes, qtds=qtds, precos={i.item_id: i.preco_unit for i in orc.itens} if orc else {},
        marcadas=marcadas, v=valores_form(orc, cfg),
        eventos=EVENTOS, status_lista=STATUS,
        clientes=Cliente.query.order_by(Cliente.nome).all(),
    )


@app.route('/orcamentos')
def orcamentos():
    tipo = request.args.get('tipo')
    status = request.args.get('status')
    consulta = Orcamento.query
    if tipo in TIPOS:
        consulta = consulta.filter_by(tipo=tipo)
    else:
        tipo = None
    if status in STATUS:
        consulta = consulta.filter_by(status=status)
    else:
        status = None
    lista = consulta.order_by(Orcamento.id.desc()).all()
    return render_template('orcamentos.html', orcamentos=lista, tipo=tipo,
                           tipos=TIPOS, status=status, status_lista=STATUS)


@app.route('/orcamentos/novo', methods=['GET', 'POST'])
def novo_orcamento():
    cfg = pegar_config()
    if request.method == 'POST':
        orc = Orcamento(numero=proximo_numero(), versao=0, fechado=0)
        if not aplicar_form(orc, request.form, cfg):
            return redirect(url_for('novo_orcamento'))
        db.session.add(orc)
        db.session.commit()
        return redirect(url_for('ver_orcamento', id=orc.id))
    modo = 'frete' if request.args.get('modo') == 'frete' else 'equip'
    return mostrar_form(None, modo, cfg)


@app.route('/orcamentos/<int:id>/editar', methods=['GET', 'POST'])
def editar_orcamento(id):
    orc = Orcamento.query.get_or_404(id)
    if orc.fechado:
        return redirect(url_for('ver_orcamento', id=id))
    cfg = pegar_config()
    if request.method == 'POST':
        if not aplicar_form(orc, request.form, cfg):
            db.session.rollback()
            return redirect(url_for('editar_orcamento', id=id))
        db.session.commit()
        return redirect(url_for('ver_orcamento', id=id))
    modo = 'frete' if orc.tipo == 'Frete' else 'equip'
    return mostrar_form(orc, modo, cfg)


@app.route('/orcamentos/<int:id>')
def ver_orcamento(id):
    orc = Orcamento.query.get_or_404(id)
    r = resumo(orc)
    texto = texto_whatsapp(r)
    link = link_whatsapp(orc.cliente.telefone, texto)
    return render_template('orcamento.html', orc=orc, r=r, texto=texto,
                           link=link, enviar=request.args.get('enviar'),
                           status_lista=STATUS)


@app.route('/orcamentos/<int:id>/fechar', methods=['POST'])
def fechar_orcamento(id):
    orc = Orcamento.query.get_or_404(id)
    if not orc.fechado:
        db.session.add(OrcamentoVersao(
            orcamento_id=orc.id, versao=orc.versao or 0,
            dados=json.dumps(resumo(orc))))
        orc.fechado = 1
        if orc.status == 'Em aberto':
            orc.status = 'Enviado'
        db.session.commit()
    return redirect(url_for('ver_orcamento', id=id, enviar=1))


@app.route('/orcamentos/<int:id>/nova-versao', methods=['POST'])
def nova_versao(id):
    orc = Orcamento.query.get_or_404(id)
    if orc.fechado:
        orc.versao = (orc.versao or 0) + 1
        orc.fechado = 0
        db.session.commit()
    return redirect(url_for('editar_orcamento', id=id))


@app.route('/orcamentos/<int:id>/versao/<int:n>')
def ver_versao(id, n):
    orc = Orcamento.query.get_or_404(id)
    ver = OrcamentoVersao.query.filter_by(orcamento_id=id, versao=n).first_or_404()
    return render_template('orcamento_versao.html', orc=orc, ver=ver,
                           r=json.loads(ver.dados))


@app.route('/orcamentos/<int:id>/controle', methods=['POST'])
def controle_orcamento(id):
    orc = Orcamento.query.get_or_404(id)
    status = request.form.get('status')
    if status in STATUS:
        orc.status = status
    orc.relembrar_em = para_data(request.form.get('relembrar_em'))
    db.session.commit()
    return redirect(url_for('ver_orcamento', id=id))


@app.route('/orcamentos/<int:id>/lembrete-feito', methods=['POST'])
def lembrete_feito(id):
    orc = Orcamento.query.get_or_404(id)
    orc.relembrar_em = None
    db.session.commit()
    return redirect(url_for('index'))


@app.route('/orcamentos/<int:id>/calendario.ics')
def calendario(id):
    orc = Orcamento.query.get_or_404(id)
    if not orc.evento_data:
        return redirect(url_for('ver_orcamento', id=id))
    resposta = Response(gerar_ics(orc), mimetype='text/calendar')
    resposta.headers['Content-Disposition'] = f'inline; filename=evento-{orc.id}.ics'
    return resposta


@app.route('/orcamentos/<int:id>/excluir', methods=['POST'])
def excluir_orcamento(id):
    db.session.delete(Orcamento.query.get_or_404(id))
    db.session.commit()
    return redirect(url_for('orcamentos'))


# ---------------------------------------------------------------
# Cria as tabelas, atualiza o banco antigo e liga o sistema
# ---------------------------------------------------------------
with app.app_context():
    db.create_all()
    atualizar_banco()


if __name__ == '__main__':
    app.run(debug=True)