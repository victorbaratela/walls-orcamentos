// 1) Liga o service worker (permite abrir telas sem internet)
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}

// 2) Fila de orçamentos feitos sem internet
const CHAVE_FILA = 'walls_fila';

function lerFila() {
  try {
    return JSON.parse(localStorage.getItem(CHAVE_FILA)) || [];
  } catch (e) {
    return [];
  }
}

function gravarFila(fila) {
  localStorage.setItem(CHAVE_FILA, JSON.stringify(fila));
  mostrarAviso();
}

function guardarNaFila(campos) {
  const fila = lerFila();
  fila.push({ campos: campos, criado: new Date().toISOString() });
  gravarFila(fila);
}

function mostrarAviso() {
  const n = lerFila().length;
  let el = document.getElementById('aviso-fila');
  if (n === 0) {
    if (el) el.remove();
    return;
  }
  if (!el) {
    el = document.createElement('div');
    el.id = 'aviso-fila';
    el.style.cssText = 'background:#f5b301;color:#14161a;padding:10px 16px;text-align:center;font-weight:bold';
    document.body.prepend(el);
  }
  el.textContent = n === 1
    ? '1 orçamento aguardando envio (será enviado quando houver internet)'
    : n + ' orçamentos aguardando envio (serão enviados quando houver internet)';
}

let sincronizando = false;

async function sincronizar() {
  if (sincronizando || lerFila().length === 0) return;
  sincronizando = true;
  let enviados = 0;
  try {
    while (lerFila().length > 0) {
      const resp = await fetch('/orcamentos/novo', {
        method: 'POST',
        body: new URLSearchParams(lerFila()[0].campos),
        credentials: 'same-origin'
      });
      if (!resp.ok || resp.url.includes('/login')) break;
      const atual = lerFila();
      atual.shift();
      gravarFila(atual);
      enviados++;
    }
  } catch (e) {
    // sem conexão de verdade: tenta de novo mais tarde
  }
  sincronizando = false;
  if (enviados > 0 && location.pathname === '/orcamentos') {
    location.reload();
  }
}

// 3) Tela de novo orçamento: tenta enviar; se não der, guarda no aparelho
const formOrc = document.getElementById('form-orc');
if (formOrc) {
  formOrc.addEventListener('submit', async ev => {
    ev.preventDefault();
    const botao = formOrc.querySelector('button[type=submit]');
    if (botao) botao.disabled = true;
    const campos = Array.from(new FormData(formOrc).entries());
    try {
      const resp = await fetch('/orcamentos/novo', {
        method: 'POST',
        body: new URLSearchParams(campos),
        credentials: 'same-origin'
      });
      if (resp.url.includes('/login')) {
        guardarNaFila(campos);
        alert('Sua sessão expirou. O orçamento foi guardado no aparelho. Entre de novo para ele ser enviado.');
        window.location.href = '/login';
        return;
      }
      if (resp.ok) {
        window.location.href = resp.url;
        return;
      }
      alert('Não foi possível salvar o orçamento. Confira os dados e tente de novo.');
    } catch (e) {
      guardarNaFila(campos);
      alert('Sem internet. O orçamento foi guardado no aparelho e será enviado quando a conexão voltar.');
      window.location.href = '/';
      return;
    }
    if (botao) botao.disabled = false;
  });
}

// 4) Quando enviar a fila
window.addEventListener('online', sincronizar);
window.addEventListener('load', () => {
  mostrarAviso();
  sincronizar();
});
setInterval(sincronizar, 30000);