from io import BytesIO
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageOps

MARCA = "Wall's"
TIPOS = ['Som', 'Iluminação', 'Som e iluminação', 'Frete']
CATEGORIAS = ['Som', 'Iluminação']
NOME_CATEGORIA = {'Som': 'Sonorização', 'Iluminação': 'Iluminação'}
EVENTOS = ['Casamento', 'Formatura', 'Aniversário', 'Festa de 15 anos',
           'Evento particular', 'Evento corporativo', 'Igreja', 'Show', 'Outro']
STATUS = ['Em aberto', 'Enviado', 'Aprovado', 'Realizado', 'Recusado', 'Cancelado']
STATUS_ATIVOS = ['Em aberto', 'Enviado', 'Aprovado']
DIAS_SEMANA = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom']

NL = chr(10)
CRLF = chr(13) + chr(10)
BARRA = chr(92)


# ---------------------------------------------------------------
# Números, datas e dinheiro
# ---------------------------------------------------------------
def para_float(texto, padrao=0.0):
    try:
        return float(str(texto).replace(',', '.'))
    except ValueError:
        return padrao


def moeda(valor):
    texto = f'{valor or 0:,.2f}'
    return 'R$ ' + texto.replace(',', 'X').replace('.', ',').replace('X', '.')


def fmt_num(valor):
    return f'{valor or 0:.2f}'.replace('.', ',')


def para_data(texto):
    try:
        return datetime.strptime(texto, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------
# Fotos
# ---------------------------------------------------------------
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


# ---------------------------------------------------------------
# Resumo do orçamento e mensagens
# ---------------------------------------------------------------
def texto_horario(o):
    if o.dia_todo and not o.hora_inicio:
        return 'Dia todo'
    if o.hora_inicio and o.hora_fim:
        return o.hora_inicio + ' às ' + o.hora_fim
    if o.hora_inicio:
        return 'a partir das ' + o.hora_inicio
    return ''


def resumo(orc):
    # Tudo o que aparece no orçamento, em um dicionário simples.
    # É também o que fica guardado em cada versão fechada.
    grupos = []
    for cat, lista, sub in orc.grupos:
        grupos.append({
            'categoria': cat,
            'subtotal': sub,
            'itens': [{'nome': i.nome, 'quantidade': i.quantidade,
                       'preco_unit': i.preco_unit, 'subtotal': i.subtotal}
                      for i in lista],
        })
    return {
        'numero': orc.numero or str(orc.id),
        'tipo': orc.tipo,
        'cliente': orc.cliente.nome,
        'telefone': orc.cliente.telefone or '',
        'emissao': f'{orc.data:%d/%m/%Y}',
        'evento_tipo': orc.evento_tipo or '',
        'evento_data': f'{orc.evento_data:%d/%m/%Y}' if orc.evento_data else '',
        'horario': texto_horario(orc),
        'local_nome': orc.local_nome or '',
        'local_endereco': orc.local_endereco or '',
        'cidade': orc.cidade or '',
        'grupos': grupos,
        'km': orc.km or 0,
        'pedagios': orc.pedagios or 0,
        'frete': orc.frete or 0,
        'combustivel': orc.custo_combustivel,
        'ajudantes': orc.ajudantes or 0,
        'valor_ajudante': orc.valor_ajudante or 0,
        'total_ajudantes': orc.total_ajudantes,
        'desconto': orc.desconto or 0,
        'observacoes': orc.observacoes or '',
        'total': orc.total,
    }


def texto_whatsapp(r):
    # Mensagem para o cliente (não inclui custos nem lucro do frete)
    L = ['*' + MARCA + ' - Orçamento ' + r['numero'] + '*',
         'Cliente: ' + r['cliente']]
    partes = [p for p in (r['evento_tipo'], r['evento_data'], r['horario']) if p]
    if partes:
        L.append('Evento: ' + ' - '.join(partes))
    local = ', '.join(p for p in (r['local_nome'], r['cidade']) if p)
    if local:
        L.append('Local: ' + local)
    L.append('')
    varios = len(r['grupos']) > 1
    for g in r['grupos']:
        if varios:
            titulo = NOME_CATEGORIA.get(g['categoria']) or g['categoria'] or 'Outros'
            L.append('*' + titulo + '*')
        for i in g['itens']:
            L.append('%dx %s' % (i['quantidade'], i['nome']))
        if varios:
            L.append('')
    if r['frete'] and r['tipo'] == 'Frete':
        rotulo = 'Frete (%g km)' % r['km'] if r['km'] else 'Frete'
        L.append(rotulo + ': ' + moeda(r['frete']))
    if r['ajudantes'] and r['tipo'] == 'Frete':
        L.append('Ajudantes (%d x %s): %s' % (
            r['ajudantes'], moeda(r['valor_ajudante']), moeda(r['total_ajudantes'])))
    if r['desconto']:
        L.append('Desconto: -' + moeda(r['desconto']))
    L += ['*Total: ' + moeda(r['total']) + '*'] if L[-1] == '' else ['', '*Total: ' + moeda(r['total']) + '*']
    if r['observacoes']:
        L += ['', r['observacoes']]
    L += ['', 'Validade: 15 dias a partir da emissão.']
    return NL.join(L)


def link_whatsapp(telefone, texto):
    fone = ''.join(c for c in (telefone or '') if c.isdigit())
    if fone and not fone.startswith('55'):
        fone = '55' + fone
    return 'https://wa.me/' + fone + '?text=' + quote(texto)


def texto_lembrete(o):
    nome = (o.cliente.nome or '').split(' ')[0]
    evento = ''
    if o.evento_data:
        evento = ' para o evento do dia ' + o.evento_data.strftime('%d/%m/%Y')
    return ('Olá, ' + nome + '! Tudo bem? Aqui é do ' + MARCA +
            '. Passando para saber se você conseguiu ver o orçamento ' +
            (o.numero or str(o.id)) + evento + '. Qualquer dúvida, é só chamar!')


# ---------------------------------------------------------------
# Agenda: conflitos de horário
# ---------------------------------------------------------------
def minutos(hhmm, padrao):
    try:
        h, m = hhmm.split(':')
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return padrao


def intervalo(o):
    # (início, fim) em minutos do dia; dia todo ocupa o dia inteiro
    if o.dia_todo or not o.hora_inicio:
        return (0, 1440)
    ini = minutos(o.hora_inicio, 0)
    fim = minutos(o.hora_fim, ini + 240)
    if fim <= ini:
        fim = 1440
    return (ini, min(fim, 1440))


def detectar_conflitos(eventos):
    # Dois eventos de clientes diferentes, na mesma data e com horários
    # que se cruzam. Orçamentos de frete não entram na conta.
    por_data = {}
    for o in eventos:
        if o.tipo != 'Frete' and o.evento_data:
            por_data.setdefault(o.evento_data, []).append(o)
    ids = set()
    for lista in por_data.values():
        for a in range(len(lista)):
            for b in range(a + 1, len(lista)):
                x, y = lista[a], lista[b]
                if x.cliente_id == y.cliente_id:
                    continue
                i1, f1 = intervalo(x)
                i2, f2 = intervalo(y)
                if i1 < f2 and i2 < f1:
                    ids.update((x.id, y.id))
    return ids


# ---------------------------------------------------------------
# Arquivo de calendário (.ics) para o celular
# ---------------------------------------------------------------
def esc_ics(s):
    s = s.replace(BARRA, BARRA * 2)
    s = s.replace(';', BARRA + ';').replace(',', BARRA + ',')
    return s.replace(NL, BARRA + 'n')


def gerar_ics(o):
    d = o.evento_data
    titulo = ' - '.join(p for p in (o.evento_tipo or 'Evento', o.cliente.nome) if p)
    local = ', '.join(p for p in (o.local_nome, o.local_endereco, o.cidade) if p)
    descricao = ('Orçamento ' + (o.numero or str(o.id)) + ' (' + (o.status or '') +
                 ') - Total ' + moeda(o.total) + (' - Horário combinado: ' + texto_horario(o) if o.hora_inicio else ''))
    linhas = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Walls//Orcamentos//PT',
              'BEGIN:VEVENT', f'UID:orc-{o.id}@walls',
              f'DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}']
    try:
        if o.dia_todo or not o.hora_inicio:
            raise ValueError('dia todo')
        ini = datetime.combine(d, datetime.strptime(o.hora_inicio, '%H:%M').time())
        if o.hora_fim:
            fim = datetime.combine(d, datetime.strptime(o.hora_fim, '%H:%M').time())
            if fim <= ini:
                fim += timedelta(days=1)
        else:
            fim = ini + timedelta(hours=4)
        linhas.append(f'DTSTART:{ini:%Y%m%dT%H%M%S}')
        linhas.append(f'DTEND:{fim:%Y%m%dT%H%M%S}')
    except ValueError:
        linhas.append(f'DTSTART;VALUE=DATE:{d:%Y%m%d}')
        linhas.append(f'DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}')
    linhas += ['SUMMARY:' + esc_ics(titulo), 'LOCATION:' + esc_ics(local),
               'DESCRIPTION:' + esc_ics(descricao), 'END:VEVENT', 'END:VCALENDAR']
    return CRLF.join(linhas) + CRLF