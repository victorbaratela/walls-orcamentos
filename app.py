import os
import hmac
from io import BytesIO
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from flask import (Flask, render_template, request, redirect,
                   url_for, session, Response)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from PIL import Image, ImageOps

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

TIPOS = ['Som', 'Iluminação', 'Som e iluminação', 'Frete']
CATEGORIAS = ['Som', 'Iluminação']
NOME_CATEGORIA = {'Som': 'Sonorização', 'Iluminação': 'Iluminação'}
app.jinja_env.globals['NOME_CATEGORIA'] = NOME_CATEGORIA


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


class Orcamento(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(20))
    cliente_id = db.Column(db.Integer, db.ForeignKey('cliente.id'), nullable=False)
    cliente = db.relationship('Cliente')
    tipo = db.Column(db.String(20), default='Som')
    data = db.Column(db.DateTime, default=agora)
    km = db.Column(db.Float, default=0)
    pedagios = db.Column(db.Float, default=0)
    frete = db.Column(db.Float, default=0)
    desconto = db.Column(db.Float, default=0)
    observacoes = db.Column(db.String(500))
    itens = db.relationship('OrcamentoItem', backref='orcamento',
                            cascade='all, delete-orphan')

    @property
    def subtotal(self):
        return sum(i.subtotal for i in self.itens)

    @property
    def total(self):
        return max(self.subtotal + (self.frete or 0) - (self.desconto or 0), 0)

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
    categoria = db.Column(db.String(30))
    nome = db.Column(db.String(120), nullable=False)
    preco_unit = db.Column(db.Float, nullable=False)
    quantidade = db.Column(db.Integer, nullable=False)

    @property
    def subtotal(self):
        return self.preco_unit * self.quantidade


# ---------------------------------------------------------------
# Funções de apoio
# ---------------------------------------------------------------
def para_float(texto, padrao=0.0):
    try:
        return float(str(texto).replace(',', '.'))
    except ValueError:
        return padrao


def moeda(valor):
    texto = f'{valor or 0:,.2f}'
    return 'R$ ' + texto.replace(',', 'X').replace('.', ',').replace('X', '.')


app.jinja_env.filters['moeda'] = moeda


def pegar_config():
    cfg = Config.query.first()
    if not cfg:
        cfg = Config()
        db.session.add(cfg)
        db.session.commit()
    return cfg


def calcular_frete(cfg, km, pedagios):
    combustivel = (km / cfg.km_por_litro * cfg.preco_litro) if cfg.km_por_litro else 0
    base = combustivel + pedagios
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


def processar_imagem(arquivo):
    # Reduz a foto para no máximo 640 px e converte para JPEG leve
    try:
        img = ImageOps.exif_transpose(Image.open(arquivo.stream))
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
            fundo = Image.new('RGB', img.size, (255, 255, 255))
            fundo.paste(img, mask=img.split()[-1])
            img = fundo
        else:
            img = img.convert('RGB')
        img.thumbnail((640, 640))
        saida = BytesIO()
        img.save(saida, 'JPEG', quality=78, optimize=True)
        return saida.getvalue()
    except Exception:
        return None


def adicionar_coluna(tabela, coluna, tipo_sql):
    colunas = [c['name'] for c in inspect(db.engine).get_columns(tabela)]
    if coluna not in colunas:
        db.session.execute(text(
            f'ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo_sql}'))
        db.session.commit()


def atualizar_banco():
    # Adiciona colunas novas em bancos criados por versões anteriores
    adicionar_coluna('orcamento', 'tipo', "VARCHAR(20) DEFAULT 'Som'")
    adicionar_coluna('orcamento', 'numero', 'VARCHAR(20)')
    adicionar_coluna('orcamento_item', 'categoria', 'VARCHAR(30)')
    adicionar_coluna('item', 'imagem_v', 'INTEGER DEFAULT 0')
    # Numera os orçamentos antigos, na ordem em que foram criados
    sem_numero = (Orcamento.query
                  .filter((Orcamento.numero.is_(None)) | (Orcamento.numero == ''))
                  .order_by(Orcamento.id).all())
    for o in sem_numero:
        o.numero = proximo_numero((o.data or agora()).year)
        db.session.flush()
    db.session.commit()


# ---------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------
@app.route('/')
def index():
    total = Orcamento.query.count()
    n_frete = Orcamento.query.filter_by(tipo='Frete').count()
    return render_template(
        'index.html',
        n_equip=total - n_frete,
        n_frete=n_frete,
        total_clientes=Cliente.query.count(),
        total_itens=Item.query.count(),
    )


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


# ----- Serviços (itens) com foto -----
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
        db.session.commit()
        return redirect(url_for('configuracoes'))
    return render_template('configuracoes.html', cfg=cfg)


# ----- Orçamentos -----
@app.route('/orcamentos')
def orcamentos():
    tipo = request.args.get('tipo')
    consulta = Orcamento.query
    if tipo in TIPOS:
        consulta = consulta.filter_by(tipo=tipo)
    else:
        tipo = None
    lista = consulta.order_by(Orcamento.id.desc()).all()
    return render_template('orcamentos.html', orcamentos=lista,
                           tipo=tipo, tipos=TIPOS)


@app.route('/orcamentos/novo', methods=['GET', 'POST'])
def novo_orcamento():
    cfg = pegar_config()
    if request.method == 'POST':
        modo = 'frete' if request.form.get('modo') == 'frete' else 'equip'
        orc = Orcamento(
            cliente_id=int(request.form['cliente_id']),
            numero=proximo_numero(),
            desconto=para_float(request.form.get('desconto')),
            observacoes=request.form.get('observacoes'),
        )
        if modo == 'frete':
            orc.tipo = 'Frete'
            orc.km = para_float(request.form.get('km'))
            orc.pedagios = para_float(request.form.get('pedagios'))
            frete_txt = request.form.get('frete', '').strip()
            if frete_txt == '':
                orc.frete = calcular_frete(cfg, orc.km, orc.pedagios)
            else:
                orc.frete = para_float(frete_txt)
        else:
            marcadas = [c for c in CATEGORIAS
                        if c in request.form.getlist('categorias')]
            if not marcadas:
                return redirect(url_for('novo_orcamento'))
            orc.tipo = marcadas[0] if len(marcadas) == 1 else 'Som e iluminação'
            for item in Item.query.filter(Item.categoria.in_(marcadas)).all():
                qtd = int(para_float(request.form.get(f'qtd_{item.id}'), 0))
                if qtd > 0:
                    orc.itens.append(OrcamentoItem(
                        nome=item.nome, categoria=item.categoria,
                        preco_unit=item.preco, quantidade=qtd))
        db.session.add(orc)
        db.session.commit()
        return redirect(url_for('ver_orcamento', id=orc.id))

    modo = 'frete' if request.args.get('modo') == 'frete' else 'equip'
    secoes = []
    if modo == 'equip':
        for cat in CATEGORIAS:
            lista = Item.query.filter_by(categoria=cat).order_by(Item.nome).all()
            secoes.append((cat, lista))
    return render_template(
        'orcamento_novo.html',
        cfg=cfg,
        modo=modo,
        secoes=secoes,
        clientes=Cliente.query.order_by(Cliente.nome).all(),
    )


@app.route('/orcamentos/<int:id>')
def ver_orcamento(id):
    orc = Orcamento.query.get_or_404(id)
    grupos = orc.grupos
    linhas = [
        f"*Wall's - Orçamento {orc.numero or orc.id}*",
        f'Cliente: {orc.cliente.nome}',
        f'Data: {orc.data:%d/%m/%Y}',
        '',
    ]
    for cat, lista, sub in grupos:
        if len(grupos) > 1:
            titulo = NOME_CATEGORIA.get(cat) or cat or 'Outros'
            linhas.append(f'*{titulo}*')
        for i in lista:
            linhas.append(f'{i.quantidade}x {i.nome} - {moeda(i.subtotal)}')
        if len(grupos) > 1:
            linhas.append(f'Subtotal: {moeda(sub)}')
            linhas.append('')
    if orc.frete:
        rotulo = f'Frete ({orc.km:g} km)' if orc.km else 'Frete'
        linhas.append(f'{rotulo}: {moeda(orc.frete)}')
    if orc.desconto:
        linhas.append(f'Desconto: -{moeda(orc.desconto)}')
    linhas += ['', f'*Total: {moeda(orc.total)}*']
    if orc.observacoes:
        linhas += ['', orc.observacoes]
    texto = '\n'.join(linhas)

    fone = ''.join(c for c in (orc.cliente.telefone or '') if c.isdigit())
    if fone and not fone.startswith('55'):
        fone = '55' + fone
    link = f'https://wa.me/{fone}?text={quote(texto)}'
    return render_template('orcamento.html', orc=orc, texto=texto, link=link)


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