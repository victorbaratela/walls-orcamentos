import os
import hmac
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from flask import (Flask, render_template, request, redirect,
                   url_for, session)
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# Banco: usa o PostgreSQL da internet se existir, senão o SQLite local
url_banco = os.environ.get('DATABASE_URL', 'sqlite:///walls.db')
if url_banco.startswith('postgres://'):
    url_banco = url_banco.replace('postgres://', 'postgresql+psycopg://', 1)
elif url_banco.startswith('postgresql://'):
    url_banco = url_banco.replace('postgresql://', 'postgresql+psycopg://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = url_banco

# Login por senha
app.secret_key = os.environ.get('SECRET_KEY', 'chave-so-para-testes-locais')
app.permanent_session_lifetime = timedelta(days=30)
SENHA = os.environ.get('SENHA_SISTEMA', 'walls123')

db = SQLAlchemy(app)

CATEGORIAS = ['Som', 'Iluminação', 'Frete']


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


class Config(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    preco_litro = db.Column(db.Float, default=6.0)
    km_por_litro = db.Column(db.Float, default=8.0)
    pedagio_padrao = db.Column(db.Float, default=0.0)
    lucro_percentual = db.Column(db.Float, default=20.0)


class Orcamento(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('cliente.id'), nullable=False)
    cliente = db.relationship('Cliente')
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
        return max(self.subtotal + self.frete - self.desconto, 0)


class OrcamentoItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    orcamento_id = db.Column(db.Integer, db.ForeignKey('orcamento.id'), nullable=False)
    nome = db.Column(db.String(120), nullable=False)
    preco_unit = db.Column(db.Float, nullable=False)
    quantidade = db.Column(db.Integer, nullable=False)

    @property
    def subtotal(self):
        return self.preco_unit * self.quantidade


# ---------------------------------------------------------------
# Funções de apoio
# ---------------------------------------------------------------
def numero(texto, padrao=0.0):
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


# ---------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------
@app.route('/')
def index():
    return render_template(
        'index.html',
        total_clientes=Cliente.query.count(),
        total_itens=Item.query.count(),
        total_orcamentos=Orcamento.query.count(),
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


@app.route('/itens', methods=['GET', 'POST'])
def itens():
    if request.method == 'POST':
        novo = Item(
            nome=request.form['nome'],
            categoria=request.form['categoria'],
            preco=numero(request.form['preco']),
        )
        db.session.add(novo)
        db.session.commit()
        return redirect(url_for('itens'))
    lista = Item.query.order_by(Item.categoria, Item.nome).all()
    return render_template('itens.html', itens=lista, categorias=CATEGORIAS)


@app.route('/itens/<int:id>/excluir', methods=['POST'])
def excluir_item(id):
    db.session.delete(Item.query.get_or_404(id))
    db.session.commit()
    return redirect(url_for('itens'))


@app.route('/configuracoes', methods=['GET', 'POST'])
def configuracoes():
    cfg = pegar_config()
    if request.method == 'POST':
        cfg.preco_litro = numero(request.form['preco_litro'])
        cfg.km_por_litro = numero(request.form['km_por_litro'], 8.0)
        cfg.pedagio_padrao = numero(request.form['pedagio_padrao'])
        cfg.lucro_percentual = numero(request.form['lucro_percentual'])
        db.session.commit()
        return redirect(url_for('configuracoes'))
    return render_template('configuracoes.html', cfg=cfg)


@app.route('/orcamentos')
def orcamentos():
    lista = Orcamento.query.order_by(Orcamento.id.desc()).all()
    return render_template('orcamentos.html', orcamentos=lista)


@app.route('/orcamentos/novo', methods=['GET', 'POST'])
def novo_orcamento():
    cfg = pegar_config()
    if request.method == 'POST':
        km = numero(request.form.get('km'))
        pedagios = numero(request.form.get('pedagios'))
        frete_txt = request.form.get('frete', '').strip()
        if frete_txt == '':
            frete = calcular_frete(cfg, km, pedagios)
        else:
            frete = numero(frete_txt)
        orc = Orcamento(
            cliente_id=int(request.form['cliente_id']),
            km=km,
            pedagios=pedagios,
            frete=frete,
            desconto=numero(request.form.get('desconto')),
            observacoes=request.form.get('observacoes'),
        )
        for item in Item.query.all():
            qtd = int(numero(request.form.get(f'qtd_{item.id}'), 0))
            if qtd > 0:
                orc.itens.append(OrcamentoItem(
                    nome=item.nome, preco_unit=item.preco, quantidade=qtd))
        db.session.add(orc)
        db.session.commit()
        return redirect(url_for('ver_orcamento', id=orc.id))
    return render_template(
        'orcamento_novo.html',
        cfg=cfg,
        clientes=Cliente.query.order_by(Cliente.nome).all(),
        itens=Item.query.filter(Item.categoria != 'Frete')
                  .order_by(Item.categoria, Item.nome).all(),
    )


@app.route('/orcamentos/<int:id>')
def ver_orcamento(id):
    orc = Orcamento.query.get_or_404(id)
    linhas = [
        f"*Wall's - Orçamento nº {orc.id}*",
        f'Cliente: {orc.cliente.nome}',
        f'Data: {orc.data:%d/%m/%Y}',
        '',
    ]
    for i in orc.itens:
        linhas.append(f'{i.quantidade}x {i.nome} - {moeda(i.subtotal)}')
    if orc.frete:
        linhas.append(f'Frete: {moeda(orc.frete)}')
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
# Cria as tabelas (se ainda não existirem) e liga o sistema
# ---------------------------------------------------------------
with app.app_context():
    db.create_all()


if __name__ == '__main__':
    app.run(debug=True)