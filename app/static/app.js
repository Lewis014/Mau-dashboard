const PAGE = 50;
let page = 0;
let currentLeadId = null;   // detalle
let tagSelected = null;     // etiquetado
// Rango de fechas de captura. Unico filtro que NO vive en el DOM: lo comparten las cuatro
// vistas, y cada una lo enseña en su chip de periodo.
let dateRange = { desde: '', hasta: '' };
let aniosCargados = false;
// El texto buscado vive en el DOM (#q-inp); esto solo guarda el temporizador del debounce,
// para no lanzar una peticion por cada tecla.
let buscarTimer = null;

/* Filtros por la fecha del siguiente paso. Deben coincidir con SEGUIMIENTO_SQL de main.py. */
const SEG_FILTROS = [
  { v: 'vencido',     l: 'Vencido' },
  { v: 'hoy',         l: 'Hoy' },
  { v: 'semana',      l: 'Esta semana' },
  { v: 'sin_definir', l: 'Sin definir' },
];

/* Etiquetas de lead: unica definicion del frontend. Filtros, badges, botones y las
   listas del dashboard se generan desde aqui; debe coincidir con TAG_GROUPS de main.py.
   v = slug guardado en BD, l = etiqueta visible, c = sufijo de las clases badge-/sel-/dot-. */
const TAG_GROUPS = [
  { key: 'estado', label: 'Estado', tags: [
    { v: 'lead_interesado',     l: 'Lead interesado',     c: 'blue' },
    { v: 'llamada',             l: 'Llamada',             c: 'cyan' },
    { v: 'llamada_no_responde', l: 'Llamada no responde', c: 'gray' },
    { v: 'insistir',            l: 'Insistir',            c: 'amber' },
    { v: 'demo_agendada',       l: 'Demo agendada',       c: 'purple' },
    { v: 'demo_realizada',      l: 'Demo realizada',      c: 'indigo' },
    { v: 'cotizacion',          l: 'Cotización',          c: 'orange' },
    { v: 'free_trial',          l: 'Free trial',          c: 'teal' },
    { v: 'cliente',             l: 'Cliente/Convertido',  c: 'green' },
    { v: 'perdido',             l: 'Perdido',             c: 'red' },
  ]},
  { key: 'responsable', label: 'Responsable', tags: [
    { v: 'alyssa', l: 'Alyssa', c: 'slate' },
    { v: 'diego',  l: 'Diego',  c: 'slate' },
    { v: 'jhon',   l: 'Jhon',   c: 'slate' },
  ]},
  { key: 'canal', label: 'Canal', tags: [
    { v: 'meta',     l: 'Meta',     c: 'brown' },
    { v: 'organico', l: 'Orgánico', c: 'brown' },
    { v: 'tiktok',   l: 'TikTok',   c: 'brown' },
  ]},
];
/* Estado comercial que trae el sync desde mau-web (app/sync_planes.py). No se edita a
   mano: es un hecho del sistema de suscripciones, no una opinion del vendedor. */
const PLAN_ESTADOS = [
  { v: 'cliente_activo',       l: 'Cliente activo',   c: 'green' },
  { v: 'ex_cliente',           l: 'Ex-cliente',       c: 'amber' },
  { v: 'trial_activo',         l: 'Prueba activa',    c: 'teal' },
  { v: 'trial_vencido',        l: 'Prueba vencida',   c: 'red' },
  { v: 'pago_en_verificacion', l: 'Pago en revisión', c: 'blue' },
  { v: 'sin_cuenta',           l: 'Sin cuenta',       c: 'gray' },
];
const PLAN_LABEL = {}, PLAN_CLASS = {};
for (const p of PLAN_ESTADOS) { PLAN_LABEL[p.v] = p.l; PLAN_CLASS[p.v] = p.c; }

// Indices planos slug → etiqueta / color / grupo, para no recorrer TAG_GROUPS en cada render.
const TAG_LABEL = {}, TAG_CLASS = {}, TAG_GROUP = {};
const TAG_ORDER = [];
for (const g of TAG_GROUPS) for (const t of g.tags) {
  TAG_LABEL[t.v] = t.l; TAG_CLASS[t.v] = t.c; TAG_GROUP[t.v] = g.key; TAG_ORDER.push(t.v);
}

/* ══════════ Utilidades ══════════ */
function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/* Icono del sprite de index.html. currentColor: toma el color de donde se pinte. */
function ico(nombre, cls = '') {
  return `<svg class="ico ${cls}" aria-hidden="true"><use href="#i-${nombre}"/></svg>`;
}

function cap(s) { return String(s ?? '').charAt(0).toUpperCase() + String(s ?? '').slice(1); }

function $(id) { return document.getElementById(id); }

/* Avatar de iniciales con color determinista: el mismo lead se ve igual en la tabla, el
   buscador, la cola de etiquetado y el detalle, sin guardar nada. */
function hueDe(s) {
  let h = 0;
  for (const ch of String(s)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h % 360;
}
function iniciales(nombre) {
  const p = String(nombre || '').trim().split(/\s+/).filter(Boolean);
  if (!p.length) return '';
  return (p[0][0] + (p[1] ? p[1][0] : '')).toUpperCase();
}
function avatar(nombre, cls = '', semilla) {
  const ini = iniciales(nombre);
  const h = hueDe(nombre || semilla || '?');
  const style = ini
    ? `--av-bg:hsl(${h} 70% 92%);--av-fg:hsl(${h} 55% 32%)`
    : '--av-bg:var(--ground-2);--av-fg:var(--ink-3)';
  return `<span class="avatar ${cls}" style="${style}" aria-hidden="true">${esc(ini || '#')}</span>`;
}
function nombreLead(l) { return l.contact_name || l.wa_display_name || ''; }

/* Anillo de probabilidad. El color sigue el umbral de probColor, y el numero va dentro. */
function scoreRing(pct, size = 32) {
  const grande = size >= 48;
  const sw = grande ? 5 : 3;
  const r = (size - sw) / 2;
  const c = 2 * Math.PI * r;
  const off = c * (1 - Math.min(100, Math.max(0, pct)) / 100);
  const m = size / 2;
  return `<span class="ring${grande ? ' ring-lg' : ''}" style="width:${size}px;height:${size}px;--ring-color:${probColor(pct)}"
      role="img" aria-label="${pct} por ciento de probabilidad de conversión" title="${pct}% de probabilidad de conversión">
    <svg viewBox="0 0 ${size} ${size}"><circle class="ring-bg" cx="${m}" cy="${m}" r="${r}"/>
      <circle class="ring-fg" cx="${m}" cy="${m}" r="${r}" stroke-dasharray="${c.toFixed(2)}" stroke-dashoffset="${off.toFixed(2)}" transform="rotate(-90 ${m} ${m})"/></svg>
    <span class="ring-txt">${pct}${grande ? '<small>%</small>' : ''}</span></span>`;
}

function probColor(pct) {
  if (pct >= 60) return 'var(--ok)';
  if (pct >= 30) return 'var(--c-amber)';
  return 'var(--ink-3)';
}

function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('es-PE', { day: '2-digit', month: 'short', year: 'numeric' });
}

/* Fechas sin hora ('YYYY-MM-DD'). fmtDate las parsearia como UTC y en Lima mostraria el
   dia anterior; el sufijo horario las ancla a hora local. */
function fmtDia(iso) { return iso ? fmtDate(iso + 'T00:00:00') : '—'; }

function fmtFechaHora(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('es-PE', {
    day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

/* Dias que lleva vencido un 'YYYY-MM-DD' (0 = hoy, negativo = aun no vence). Se compara
   por dia y no por instante: un paso para hoy no esta vencido a las 11 de la noche. */
function diasDesde(iso) {
  const hoy = new Date(); hoy.setHours(0, 0, 0, 0);
  return Math.round((hoy - new Date(iso + 'T00:00:00')) / 86400000);
}

function enCampo(el) {
  return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable);
}

/* ══════════ Toasts ══════════ */
/* Sustituyen a alert(): no bloquean y no paran lo que se estaba haciendo. */
const TOAST_ICONO = { ok: 'check-circle', crit: 'x-circle', info: 'info' };
function toast(mensaje, tipo = 'info', ms = 4000) {
  const el = document.createElement('div');
  el.className = `toast toast-${tipo}`;
  el.setAttribute('role', tipo === 'crit' ? 'alert' : 'status');
  el.innerHTML = `${ico(TOAST_ICONO[tipo] || 'info')}<div>${esc(mensaje)}</div>`;
  $('toasts').appendChild(el);
  setTimeout(() => {
    el.classList.add('is-leaving');
    setTimeout(() => el.remove(), 220);
  }, ms);
}

/* ══════════ Sesion ══════════ */
function token() { return localStorage.getItem('mau_tk') || ''; }
function logout() { localStorage.removeItem('mau_tk'); location.reload(); }

/* El usuario va dentro del token ('usuario:caducidad:firma', ver session_user en main.py).
   Se lee SOLO para mostrarlo: quien manda sigue siendo la firma que valida el servidor, asi
   que manipularlo aqui no da acceso a nada, solo cambia lo que uno mismo ve. */
function usuarioActual() {
  try {
    return atob(token().replace(/-/g, '+').replace(/_/g, '/')).split(':')[0] || '';
  } catch { return ''; }
}

function renderUsuario() {
  const u = usuarioActual();
  $('header-user').hidden = !u;
  $('user-chip').textContent = u ? cap(u) : '';
  $('user-avatar').textContent = u ? u.charAt(0).toUpperCase() : '';
  // Sin sesion no se enseña un buscador que no puede buscar nada, ni una campana vacia.
  $('search').hidden = !u;
  $('bell').hidden = !u;
}

/* ══════════ Barra lateral, menus y drawer ══════════ */
function toggleUserMenu(e) {
  e.stopPropagation();
  const menu = $('user-menu');
  const abrir = menu.hidden;
  cerrarMenus();
  menu.hidden = !abrir;
  $('user-btn').setAttribute('aria-expanded', abrir ? 'true' : 'false');
}
function cerrarMenus() {
  $('user-menu').hidden = true;
  $('user-btn').setAttribute('aria-expanded', 'false');
}

/* Preferencia guardada: 'collapsed' fuerza el riel, 'expanded' fuerza el menu completo
   (por encima del riel automatico de laptop), vacio deja el ancho decidir. El umbral de
   1440 es el mismo que el @media de styles.css: por debajo, riel salvo que se pida lo otro. */
function aplicarSidebar() {
  const pref = localStorage.getItem('mau_sidebar') || '';
  const shell = $('shell');
  shell.classList.toggle('collapsed', pref === 'collapsed');
  shell.classList.toggle('expanded', pref === 'expanded');
  const riel = pref === 'collapsed' || (window.innerWidth < 1440 && pref !== 'expanded');
  const btn = $('collapse-btn');
  btn.title = btn.ariaLabel = riel ? 'Expandir menú' : 'Contraer menú';
}
function toggleSidebar() {
  const shell = $('shell');
  const riel = shell.classList.contains('collapsed')
    || (window.innerWidth < 1440 && !shell.classList.contains('expanded'));
  localStorage.setItem('mau_sidebar', riel ? 'expanded' : 'collapsed');
  aplicarSidebar();
}
function abrirDrawer() { $('shell').classList.add('drawer-open'); }
function cerrarDrawer() { $('shell').classList.remove('drawer-open'); }
function toggleSearchMobile() {
  const abierto = $('topbar').classList.toggle('search-open');
  if (abierto) { $('q-inp').focus(); }
}
function enfocarBuscador() {
  if (window.matchMedia('(max-width: 899px)').matches) $('topbar').classList.add('search-open');
  const inp = $('q-inp');
  inp.focus();
  inp.select();
}

/* ══════════ Modales ══════════ */
function abrirModal(id) {
  const o = $(id);
  o.classList.add('open');
  const primero = o.querySelector('input:not([type=hidden]):not([disabled]), textarea');
  if (primero) primero.focus();
}
function cerrarModal(id) { $(id).classList.remove('open'); }

/* Escape cierra lo que este abierto, de lo mas efimero (popover) a lo mas pesado (modal).
   El login no se cierra: sin sesion no hay a donde volver. */
function onEscape() {
  cerrarTagPops();
  cerrarPeriodo();
  ocultarResultados();
  cerrarMenus();
  cerrarFiltros();
  const abiertos = [...document.querySelectorAll('.overlay.open')].filter(o => o.id !== 'auth-overlay');
  if (abiertos.length) abiertos[abiertos.length - 1].classList.remove('open');
}

/* Ojo para ver lo que se escribe en los campos de contraseña. Se aplica de una vez a todos
   (el del login y los tres del cambio) en lugar de repetir el mismo marcado cuatro veces.
   Importa cuando la contraseña la escribe otra persona al dictado, que es justo el caso de
   la credencial inicial que reparte el administrador. */
function montarOjosClave() {
  document.querySelectorAll('input[type="password"]').forEach(inp => {
    const wrap = document.createElement('div');
    wrap.className = 'pw-wrap';
    inp.parentNode.insertBefore(wrap, inp);
    wrap.appendChild(inp);

    const btn = document.createElement('button');
    // type=button es obligatorio: dentro de un <form>, un <button> sin tipo envia el
    // formulario, asi que pulsar el ojo intentaria iniciar sesion.
    btn.type = 'button';
    btn.className = 'pw-ojo';
    btn.tabIndex = -1;   // fuera del recorrido con Tab: estorba entre usuario y contraseña
    const pintar = () => {
      const oculta = inp.type === 'password';
      btn.innerHTML = ico(oculta ? 'eye' : 'eye-off');
      // Sin texto dentro del boton, title y aria-label son la unica pista de que hace.
      btn.title = btn.ariaLabel = oculta ? 'Mostrar contraseña' : 'Ocultar contraseña';
    };
    btn.onclick = () => {
      inp.type = inp.type === 'password' ? 'text' : 'password';
      pintar();
      inp.focus();
    };
    pintar();
    wrap.appendChild(btn);
  });
}

/* ══════════ Cambio de contraseña ══════════ */
function openClave() {
  cerrarMenus();
  $('clave-quien').textContent = cap(usuarioActual()) || 'tu usuario';
  ['clave-actual', 'clave-nueva', 'clave-rep'].forEach(id => {
    const inp = $(id);
    inp.value = '';
    // Si quedaron a la vista la vez anterior, se vuelven a ocultar.
    if (inp.type === 'text') inp.parentNode.querySelector('.pw-ojo').click();
  });
  $('clave-err').hidden = true;
  abrirModal('clave-overlay');
}

function closeClave() { cerrarModal('clave-overlay'); }

async function doCambiarClave(e) {
  e.preventDefault();
  const val = id => $(id).value;
  const err = $('clave-err');
  const fallo = m => { err.textContent = m; err.hidden = false; };
  const [actual, nueva, rep] = ['clave-actual', 'clave-nueva', 'clave-rep'].map(val);

  // Lo que se puede comprobar sin servidor, se comprueba aqui: repetirla mal es el error
  // mas frecuente y no merece un viaje de ida y vuelta.
  if (nueva !== rep) return fallo('La nueva contraseña y su repetición no coinciden.');
  if (nueva.length < 8) return fallo('La nueva contraseña debe tener al menos 8 caracteres.');
  if (nueva === actual) return fallo('La nueva contraseña es igual a la actual.');

  const btn = $('clave-btn');
  btn.disabled = true; btn.textContent = 'Guardando…';
  const r = await api('POST', '/password', { actual, nueva });
  btn.disabled = false; btn.textContent = 'Guardar';
  if (!r) return;                        // sesion caducada: api() ya mostro el login
  if (!r.ok) return fallo(r.detail || 'No se pudo cambiar la contraseña.');
  closeClave();
  toast('Contraseña cambiada. La próxima vez que entres, usa la nueva.', 'ok', 6000);
}

async function doLogin(e) {
  e.preventDefault();
  const username = $('user-inp').value.trim();
  const password = $('pass-inp').value;
  if (!username || !password) return;
  const btn = $('login-btn');
  btn.disabled = true; btn.textContent = 'Ingresando…';
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.token) {
      const err = $('auth-err');
      err.textContent = data.detail || 'Credenciales incorrectas. Vuelve a intentarlo.';
      err.hidden = false;
      return;
    }
    localStorage.setItem('mau_tk', data.token);
    $('auth-err').hidden = true;
    $('auth-overlay').classList.remove('open');
    renderUsuario();
    route();
  } finally {
    btn.disabled = false; btn.textContent = 'Ingresar';
  }
}

/* Cierra la sesion y vuelve a pedir la clave. Aparte de api() porque la exportacion a
   CSV no pasa por ahi (descarga un fichero, no JSON) y un 401 tiene que echar igual. */
function sesionExpirada() {
  localStorage.removeItem('mau_tk');
  const err = $('auth-err');
  err.textContent = 'Tu sesión expiró. Vuelve a iniciar sesión.';
  err.hidden = false;
  $('auth-overlay').classList.add('open');
  renderUsuario();
}

async function api(method, path, body) {
  const res = await fetch('/api' + path, {
    method,
    headers: { 'Authorization': 'Bearer ' + token(), 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && path !== '/password') {
    // /password se excluye: ahi un 401 significa «la contraseña actual no es correcta»,
    // no que la sesion caducara. Cerrarla seria expulsar a alguien por una errata.
    sesionExpirada();
    return null;
  }
  return res.json();
}

/* ══════════ Router ══════════ */
const VIEWS = ['dashboard', 'leads', 'detail', 'etiquetado', 'scoreboard'];
const TITULOS = { dashboard: 'Dashboard', leads: 'Leads', detail: 'Detalle del lead', etiquetado: 'Etiquetado', scoreboard: 'Scoreboard' };

function nav(view, param) {
  location.hash = param ? `#${view}/${param}` : `#${view}`;
}

function vistaActual() {
  return (location.hash.replace(/^#/, '') || 'dashboard').split('/')[0];
}

function route() {
  if (!token()) return;
  const [view, param] = (location.hash.replace(/^#/, '') || 'dashboard').split('/');
  const v = VIEWS.includes(view) ? view : 'dashboard';
  VIEWS.forEach(x => { $('view-' + x).hidden = x !== v; });
  document.querySelectorAll('.nav-item').forEach(el => {
    const on = el.dataset.view === v || (v === 'detail' && el.dataset.view === 'leads');
    el.classList.toggle('active', on);
    if (on) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current');
  });
  $('topbar-title').textContent = TITULOS[v];
  // Solo la tabla de leads ocupa la altura sobrante y desplaza dentro de su caja.
  $('main').classList.toggle('fit', v === 'leads');
  $('main').scrollTop = 0;
  window.scrollTo(0, 0);
  cerrarDrawer(); cerrarTagPops(); cerrarPeriodo(); ocultarResultados(); cerrarMenus();
  renderRangeChip();
  if (v === 'dashboard') loadDashboard();
  if (v === 'leads') loadLeads();
  if (v === 'detail') loadDetail(param);
  if (v === 'etiquetado') loadEtiquetado();
  if (v === 'scoreboard') loadScoreboard();
  refreshTagBadge();
  renderCampana();   // usa la cache: no reevalua en cada cambio de vista
}
window.addEventListener('hashchange', route);

/* ══════════ Dashboard ══════════ */
function kpi({ label, valor, pie, icono, cls = '', onclick = '', title = '' }) {
  const tag = onclick ? 'button' : 'div';
  return `<${tag} class="kpi${onclick ? ' kpi-link' : ''} ${cls}"${onclick ? ` type="button" onclick="${onclick}"` : ''}${title ? ` title="${esc(title)}"` : ''}>
      <div class="kpi-head"><span class="kpi-label">${label}</span>${ico(icono)}</div>
      <div class="kpi-value">${valor}</div>
      <div class="kpi-foot"><span>${pie}</span>${onclick ? ico('arrow-right') : ''}</div>
    </${tag}>`;
}

function skeletonKpis() {
  return Array(5).fill('<div class="kpi skeleton-tile skeleton"></div>').join('');
}

async function loadDashboard() {
  if (!$('kpi-grid').children.length) $('kpi-grid').innerHTML = skeletonKpis();
  const s = await api('GET', '/stats?' + aplicarRango(new URLSearchParams()));
  if (!s) return;
  poblarAnios(s.primer_lead);
  renderFrescura(s.fuente);

  const prob = s.prob_promedio != null ? Math.round(s.prob_promedio * 100) + '<small>%</small>' : '—';
  const vencidos = s.seguimientos_vencidos || 0;
  $('kpi-grid').innerHTML = [
    kpi({ label: 'Total de leads', valor: s.total, pie: `${s.con_transcript} con conversación`, icono: 'users' }),
    kpi({ label: 'Calificados', valor: s.calificados, pie: `${s.total ? Math.round(s.calificados / s.total * 100) : 0} % del total`, icono: 'target' }),
    kpi({ label: 'Clientes', valor: s.clientes, pie: 'conversiones confirmadas', icono: 'award' }),
    kpi({ label: 'Prob. de conversión', valor: prob, pie: `promedio de ${s.con_score} lead${s.con_score !== 1 ? 's' : ''} puntuados`, icono: 'gauge' }),
    kpi({ label: 'Seguimientos vencidos', valor: vencidos, pie: 'siguiente paso con fecha pasada', icono: 'clock',
          cls: vencidos ? 'kpi-crit' : '', onclick: "goLeadsSeguimiento('vencido')",
          title: 'Ver los leads con el siguiente paso vencido' }),
  ].join('');

  loadAlertaBox();

  const porPlan = s.por_plan_estado || {};
  const sincronizado = Object.values(porPlan).reduce((a, b) => a + b, 0);
  $('plan-grid').innerHTML = renderEmbudo(porPlan, sincronizado);

  const porTag = s.por_tag || {};
  // Un lead con varias etiquetas cuenta en cada una: los grupos no suman el total, y por
  // eso el pendiente de etiquetar va como lista aparte al final.
  $('outcome-grid').innerHTML = TAG_GROUPS.map(g => barlist(g.label,
    g.tags.map(t => ({ l: t.l, c: t.c, n: porTag[t.v] || 0, onclick: `goLeadsFiltered('${t.v}')` })),
  )).join('') + barlist('Pendientes', [
    { l: 'Sin etiquetar', c: 'gray', n: s.sin_etiquetas || 0, onclick: "nav('etiquetado')", pendiente: true },
  ], 'con conversación y sin etiqueta');
}

/* Barra apilada: un segmento por estado, proporcional a cuantos leads hay en el. Debajo,
   la leyenda con el numero, para que la barra se lea y ademas se pueda contar. */
function renderEmbudo(porPlan, sincronizado) {
  if (!sincronizado) {
    return `<div class="empty">${ico('refresh')}
      <p>Todavía no se ha sincronizado con MAU Comunica. Trae el estado comercial de cada lead para ver el embudo.</p>
      <button type="button" class="btn btn-secondary btn-sm" onclick="syncPlanes()">Sincronizar planes</button></div>`;
  }
  const segs = PLAN_ESTADOS.map(p => ({ ...p, n: porPlan[p.v] || 0 }));
  const pct = n => Math.round(n / sincronizado * 100);
  return `<div class="stack">${segs.filter(s => s.n).map(s => `
      <button type="button" class="stack-seg dot-${s.c}" style="flex:${s.n} 1 0" title="${esc(s.l)}: ${s.n} (${pct(s.n)} %)"
              aria-label="${esc(s.l)}: ${s.n}" onclick="goLeadsPlan('${s.v}')"></button>`).join('')}</div>
    <div class="legend">${segs.map(s => `
      <button type="button" class="legend-item" onclick="goLeadsPlan('${s.v}')">
        <i class="dot dot-${s.c}"></i>${esc(s.l)}<b>${s.n}</b><span class="hint">${pct(s.n)} %</span>
      </button>`).join('')}</div>`;
}

/* Lista con barra: cada fila pinta su proporcion respecto al mayor del grupo. Se lee de
   un vistazo cual pesa mas, cosa que una rejilla de numeros sueltos no dice. */
function barlist(titulo, filas, sub = '') {
  const max = Math.max(1, ...filas.map(f => f.n));
  return `<div class="card barlist-card">
      <h4>${esc(titulo)}${sub ? `<span>${esc(sub)}</span>` : ''}</h4>
      <div class="barlist">${filas.map(f => `
        <button type="button" class="barlist-row${f.pendiente ? ' is-pending' : ''}" onclick="${f.onclick}">
          <span class="barlist-label"><i class="dot dot-${f.c}"></i><span>${esc(f.l)}</span></span>
          <span class="barlist-bar"><span class="barlist-fill" style="width:${Math.round(f.n / max * 100)}%"></span></span>
          <b class="barlist-n">${f.n}</b>
        </button>`).join('')}</div>
    </div>`;
}

/* Deja solo el filtro pedido activo y limpia los demas, para que al llegar desde el
   dashboard el conteo de la tabla cuadre con el numero que se pulso. */
function goLeadsFiltered(tag) {
  for (const g of TAG_GROUPS) {
    $(`f-tag-${g.key}`).value = g.key === TAG_GROUP[tag] ? tag : '';
  }
  $('f-plan').value = '';
  $('f-seg').value = '';
  page = 0;
  nav('leads');
}

function goLeadsPlan(estado) {
  for (const g of TAG_GROUPS) $(`f-tag-${g.key}`).value = '';
  $('f-plan').value = estado;
  $('f-seg').value = '';
  page = 0;
  nav('leads');
}

function goLeadsSeguimiento(valor) {
  for (const g of TAG_GROUPS) $(`f-tag-${g.key}`).value = '';
  $('f-plan').value = '';
  $('f-seg').value = valor;
  page = 0;
  nav('leads');
}

/* ══════════ Frescura de los datos ══════════ */

/* Cuánto hace, en palabras. «20 ago 2026» no dice si eso es normal; «hace 3 días», sí. */
function hace(horas) {
  if (horas == null) return 'nunca';
  if (horas < 1) return 'hace menos de una hora';
  if (horas < 48) { const h = Math.round(horas); return `hace ${h} hora${h !== 1 ? 's' : ''}`; }
  return `hace ${Math.floor(horas / 24)} días`;
}

function botonTraer() {
  return `<button type="button" class="btn btn-secondary btn-sm" id="traer-btn" onclick="traerLeads()">${ico('download', 'ico-sm')}<span>Traer leads nuevos</span></button>`;
}

/* Se pinta encima de los KPIs y con color propio cuando algo va mal. El fallo que motivó
   esto fue que la entrada de leads estuvo tres días parada y el dashboard se veía idéntico
   a un día normal: el total seguía ahí, tan tranquilo. */
function renderFrescura(f) {
  const el = $('frescura');
  if (!f) { el.innerHTML = ''; el.className = ''; return; }

  if (!f.conocido) {
    el.className = 'frescura';
    el.innerHTML = `<div class="frescura-txt"><span class="status-dot" style="background:var(--ink-3);box-shadow:0 0 0 3px var(--ground-2)"></span>
      <span>Todavía no se ha revisado la fuente. La primera pasada corre sola a la hora programada; también puedes lanzarla ahora.</span></div>` + botonTraer();
  } else if (f.fallo) {
    el.className = 'frescura frescura-mal';
    el.innerHTML = `<div class="frescura-txt">${ico('alert')}<span><b>La última actualización falló</b>
      (${esc(fmtFechaHora(f.ultima_pasada))}). Motivo: ${esc(f.fallo)}.
      Lo que ves es lo último que sí se pudo traer.</span></div>` + botonTraer();
  } else if (f.muda) {
    el.className = 'frescura frescura-mal';
    // Se dice el hecho y se dejan las dos causas abiertas. Desde aquí NO se puede distinguir
    // «no escribe nadie» de «se cortó la conexión»: lo único que se ve es que el inbox no se
    // mueve. La primera vez que esto saltó, la causa era una campaña de anuncios parada, y
    // dar por hecho que estaba roto habría mandado a buscar la avería al sitio equivocado.
    // Sí se puede afirmar que el inbox se leyó bien: esta rama solo se alcanza sin fallo.
    const leidas = f.conversaciones != null
      ? `El inbox se leyó correctamente (${f.conversaciones} conversaciones), pero ninguna tiene mensajes nuevos`
      : 'El inbox se leyó correctamente, pero ninguna conversación tiene mensajes nuevos';
    el.innerHTML = `<div class="frescura-txt">${ico('alert')}<span>` + (f.ultima_actividad
      ? `<b>Sin actividad desde el ${esc(fmtFechaHora(f.ultima_actividad))}</b>
         (${hace(f.horas_sin_actividad)}). ${leidas}: o no está escribiendo nadie, o algo se
         cortó antes de llegar a Chatwoot.`
      : `<b>No se encontró ninguna conversación en el inbox.</b>
         Eso no es un día tranquilo: o cambió la conexión con Chatwoot, o no se está mirando
         el inbox correcto.`) + '</span></div>' + botonTraer();
  } else {
    el.className = 'frescura';
    el.innerHTML = `<div class="frescura-txt"><span class="status-dot"></span>
      <span>Datos al día · última conversación ${hace(f.horas_sin_actividad)} · revisado ${esc(fmtFechaHora(f.ultima_pasada))}</span></div>`
      + botonTraer();
  }
}

/* Lanza la pasada y sondea hasta que acabe. El POST no espera el resultado: el barrido tarda
   minutos (cientos de conversaciones y una llamada a Claude por cada una que cambió), así que
   el servidor responde al instante y el avance se consulta aparte. */
let traerTimer = null;

async function traerLeads() {
  const btn = $('traer-btn');
  if (!btn || btn.disabled) return;
  const lbl = btn.querySelector('span') || btn;
  btn.disabled = true;
  lbl.textContent = 'Trayendo…';

  const r = await api('POST', '/backfill');
  if (!r || !r.corriendo) {
    toast('No se pudo iniciar: ' + ((r && r.detail) || 'error de conexión'), 'crit', 6000);
    btn.disabled = false; lbl.textContent = 'Traer leads nuevos';
    return;
  }

  const inicio = Date.now();
  clearInterval(traerTimer);
  traerTimer = setInterval(async () => {
    const e = await api('GET', '/backfill/estado');
    if (!e || e.corriendo) {
      // El botón se repinta si alguien recarga el dashboard mientras tanto; comprobarlo
      // evita escribir sobre un nodo que ya no está en la página.
      const min = Math.floor((Date.now() - inicio) / 60000);
      if (btn.isConnected) lbl.textContent = min ? `Trayendo… (${min} min)` : 'Trayendo…';
      return;
    }
    clearInterval(traerTimer);
    traerTimer = null;
    if (e.estado === 'error') {
      toast('La actualización falló: ' + (e.detalle || 'motivo desconocido'), 'crit', 8000);
    } else if (e.resultado) {
      const x = e.resultado;
      toast(`Actualización lista.\n${x.nuevos} lead(s) nuevos o con la conversación cambiada. `
          + `${x.sin_cambios} sin cambios y ${x.descartados} descartados por no llegar a ser una conversación. `
          + `Se revisaron ${x.conversaciones} conversaciones del inbox.`, 'ok', 10000);
    }
    loadDashboard();   // repinta los KPIs y la banda con lo que se acaba de traer
  }, 5000);
}

/* ══════════ Alertas automáticas ══════════ */

/* Una sola consulta alimenta la campanita y el panel del dashboard. Se guarda unos minutos
   porque route() se dispara en cada cambio de vista y no hace falta reevaluar tan seguido:
   los datos los refresca el sync una vez al día. */
let alertasCache = { at: 0, datos: null };
let alertasEnVuelo = null;
let verTodasAlertas = false;
const ALERTAS_TTL = 5 * 60 * 1000;

async function cargarAlertas(forzar) {
  if (!forzar && alertasCache.datos && Date.now() - alertasCache.at < ALERTAS_TTL) {
    return alertasCache.datos;
  }
  // Al abrir el dashboard, la campanita y el panel piden lo mismo a la vez: sin esto
  // saldrian dos peticiones identicas cada vez.
  if (!forzar && alertasEnVuelo) return alertasEnVuelo;
  alertasEnVuelo = (async () => {
    const a = await api('POST', '/alertas/preview');
    if (a && !a.detail) alertasCache = { at: Date.now(), datos: a };
    return a;
  })();
  try {
    return await alertasEnVuelo;
  } finally {
    alertasEnVuelo = null;
  }
}

/* Las mías. El filtro es del lado del cliente a propósito: la tabla de leads ya enseña
   todos los leads a todo el mundo, así que aquí no se expone nada nuevo. El admin las ve
   todas porque no es responsable de ninguna. */
function misAlertas(a) {
  const yo = usuarioActual();
  // El admin no es responsable de ningún lead, así que las ve todas o no vería ninguna.
  return yo === 'admin' ? a.alertas : a.alertas.filter(x => x.responsable === yo);
}

async function renderCampana(forzar) {
  const badge = $('bell-badge');
  const a = await cargarAlertas(forzar);
  if (!a || a.detail) { badge.hidden = true; return; }
  const n = misAlertas(a).length;
  badge.hidden = !n;
  badge.textContent = n > 99 ? '99+' : n;
  $('bell').title = n
    ? `${n} lead${n !== 1 ? 's' : ''} que necesitan algo hoy`
    : 'Nada pendiente hoy';
}

/* La regla queda escrita en la pantalla, con los umbrales y los canales REALES que devuelve
   el servidor, no unos valores copiados a mano aquí: si alguien cambia ALERTAS_TRIAL_DIAS o
   se queda sin configurar el correo, lo que se lee en el dashboard cambia con ello. */
async function loadAlertaBox() {
  const el = $('alerta-box');
  const a = await cargarAlertas();
  if (!a) return;
  if (a.detail) {
    el.innerHTML = `${ico('alert')}<div class="notice-body">No se pudieron evaluar las alertas: ${esc(a.detail)}</div>`;
    return;
  }
  const u = a.umbrales;
  const canal = (a.canales || []).length
    ? `Cada día a las <b>${String(a.hora).padStart(2, '0')}:00</b> se avisa por <b>${esc(a.canales.join(' y '))}</b>`
    : `<b>Sin canal configurado:</b> las reglas se evalúan y salen en la campanita, pero <b>no se envía nada</b>. Se avisaría`;
  const frase = n => `${n} lead${n !== 1 ? 's' : ''}`;
  const huerfanos = a.sin_dueno
    ? ` ${frase(a.sin_dueno)} cumplen la regla pero no tienen responsable, así que no generan alerta.`
    : '';
  const sinCorreo = (a.sin_correo || []).length
    ? ` <b style="color:var(--crit)">Sin correo configurado: ${esc(a.sin_correo.map(cap).join(', '))}</b> — no reciben nada.`
    : '';
  el.innerHTML = `${ico('bell')}<div class="notice-body">
      ${canal} de las pruebas que vencen en <b>${u.trial_dias} día${u.trial_dias !== 1 ? 's' : ''} o menos</b>
      y de los pagos que llevan <b>${u.pago_dias} día${u.pago_dias !== 1 ? 's' : ''} o más</b> sin aprobarse.
      Cada persona recibe <b>solo sus leads</b>.${huerfanos}${sinCorreo}
      <div class="notice-actions">
        <button type="button" class="btn btn-secondary btn-sm" onclick="verAlertas()">
          Ver alertas de hoy${a.alertas.length ? ` (${a.alertas.length})` : ''}</button>
      </div></div>`;
}

function alertaItem(x) {
  const detalle = x.tipo === 'trial_por_vencer'
    ? `Prueba ${x.dias === 0 ? '<b>vence hoy</b>' : `vence en <b>${x.dias} día${x.dias !== 1 ? 's' : ''}</b>`} (${esc(fmtDia(x.fecha))})`
    : `Pago esperando aprobación desde hace <b>${x.dias} días</b> (${esc(fmtDia(x.fecha))})`;
  return `<button type="button" class="alerta-item" onclick="irAlerta('${esc(x.lead_id)}')">
      <div><b>${esc(x.nombre)}</b>${x.empresa ? ' · ' + esc(x.empresa) : ''}
        <code>+${esc(x.telefono)}</code></div>
      <div class="hint">${detalle}</div>
      ${x.siguiente_paso ? `<div class="hint">Siguiente paso: ${esc(x.siguiente_paso)}</div>` : ''}
    </button>`;
}

function irAlerta(leadId) {
  closeAlertas();
  nav('detail', leadId);
}

/* forzar=true al abrir la campanita (datos frescos); false al solo cambiar el filtro, que
   no necesita volver a preguntar por lo mismo. */
async function verAlertas(forzar = true) {
  const body = $('alertas-body');
  body.innerHTML = '<div class="modal-loading"><span class="spinner"></span>Evaluando…</div>';
  $('alertas-overlay').classList.add('open');
  const a = await cargarAlertas(forzar);
  if (!a) return;
  if (a.detail) { body.innerHTML = `<div class="empty">${ico('alert')}<p>${esc(a.detail)}</p></div>`; return; }

  const yo = usuarioActual();
  const mias = misAlertas(a);
  const lista = verTodasAlertas ? a.alertas : mias;
  const otras = a.alertas.length - mias.length;

  if (!lista.length) {
    body.innerHTML = `<div class="empty">${ico('check-circle')}<p>Nada pendiente ${verTodasAlertas ? '' : 'para ti '}hoy.</p>
      ${otras && !verTodasAlertas ? `<button type="button" class="btn btn-secondary btn-sm" onclick="alternarAlertas()">Ver las de todo el equipo (${otras})</button>` : ''}</div>`;
    return;
  }
  const duenos = [...new Set(lista.map(x => x.responsable))].sort();
  const porDueno = duenos.length > 1;
  body.innerHTML = `<div class="alerta-lista">
    ${porDueno
      ? duenos.map(d => `<h4>${esc(cap(d))} — ${lista.filter(x => x.responsable === d).length} por atender</h4>
          ${lista.filter(x => x.responsable === d).map(alertaItem).join('')}`).join('')
      : `<h4>${esc(cap(duenos[0] || yo))} — ${lista.length} por atender</h4>${lista.map(alertaItem).join('')}`}
    <div class="hint" style="margin-top:16px">
      Haz clic en un lead para abrirlo. El contador baja cuando el lead deja de cumplir la
      regla, no al mirarlo.
      ${otras ? `<br><button type="button" class="btn btn-ghost btn-sm" style="margin-top:8px" onclick="alternarAlertas()">${
        verTodasAlertas ? 'Ver solo las mías' : `Ver las de todo el equipo (${otras} más)`}</button>` : ''}
    </div>
  </div>`;
}

function alternarAlertas() {
  verTodasAlertas = !verTodasAlertas;
  return verAlertas(false);   // devuelve la promesa: repintar es asincrono
}

function closeAlertas() { cerrarModal('alertas-overlay'); }

/* Trae de mau-web el estado comercial de todos los leads. Tarda unos segundos (una
   llamada HTTP mas un UPDATE por lead cruzado), asi que el boton se bloquea mientras. */
async function syncPlanes() {
  const btn = $('sync-btn');
  const html = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = ico('refresh', 'ico-sm') + 'Sincronizando…';
  try {
    const r = await api('POST', '/sync-planes');
    if (!r || r.detail) {
      toast('No se pudo sincronizar: ' + ((r && r.detail) || 'error de conexión con MAU Comunica'), 'crit', 7000);
      return;
    }
    const amb = r.ambiguos ? ` ${r.ambiguos} descartados por tener el teléfono o correo repetido en varias cuentas.` : '';
    toast(`Sincronización lista. ${r.cruzados} de ${r.leads} leads cruzaron con una cuenta`
        + ` (${r.por_telefono} por teléfono, ${r.por_correo} por correo). `
        + `${r.sin_cuenta} no tienen cuenta en MAU Comunica.${amb}`, 'ok', 9000);
    loadDashboard();
  } finally {
    btn.disabled = false; btn.innerHTML = html;
  }
}

/* ══════════ Periodo (compartido por las cuatro vistas) ══════════ */

function aplicarRango(params) {
  if (dateRange.desde) params.set('desde', dateRange.desde);
  if (dateRange.hasta) params.set('hasta', dateRange.hasta);
  return params;
}

/* Fecha -> 'YYYY-MM-DD' en hora local. No usar toISOString(): convierte a UTC y
   en Lima (UTC-5) devolveria el dia anterior. */
function isoDia(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/* El selector de años cubre desde el primer lead registrado hasta hoy. Se puebla una
   sola vez; /api/stats devuelve primer_lead sin aplicar el filtro de fecha. */
function poblarAnios(primerLead) {
  if (aniosCargados) return;
  const sel = $('d-anio');
  const hasta = new Date().getFullYear();
  const desde = primerLead ? new Date(primerLead).getFullYear() : hasta;
  for (let a = hasta; a >= desde; a--) {
    sel.insertAdjacentHTML('beforeend', `<option value="${a}">${a}</option>`);
  }
  aniosCargados = true;
}

/* Atajo Mes/Año: reescribe desde/hasta. Mes sin año usa el año en curso; año sin mes
   toma el año completo. */
function onAtajoMesAnio() {
  const vMes  = $('d-mes').value;
  const vAnio = $('d-anio').value;
  if (!vMes && !vAnio) return limpiarRango();

  const mes  = parseInt(vMes, 10);
  const anio = parseInt(vAnio, 10) || new Date().getFullYear();
  const ini  = mes ? new Date(anio, mes - 1, 1) : new Date(anio, 0, 1);
  const fin  = mes ? new Date(anio, mes, 0)     : new Date(anio, 11, 31);

  dateRange = { desde: isoDia(ini), hasta: isoDia(fin) };
  $('d-desde').value = dateRange.desde;
  $('d-hasta').value = dateRange.hasta;
  recargarPorRango();
}

/* Edicion manual de desde/hasta: el atajo deja de describir el rango, se limpia. */
function onRangoManual() {
  dateRange = {
    desde: $('d-desde').value,
    hasta: $('d-hasta').value,
  };
  $('d-mes').value = '';
  $('d-anio').value = '';
  recargarPorRango();
}

function limpiarRango() {
  dateRange = { desde: '', hasta: '' };
  ['d-desde', 'd-hasta', 'd-mes', 'd-anio'].forEach(id => { $(id).value = ''; });
  cerrarPeriodo();
  recargarPorRango();
}

function atajoPeriodo(que) {
  if (que === 'hoy') {
    const hoy = isoDia(new Date());
    ponerRango(hoy, hoy);
  }
}

/* Mes natural con `offset` (0 = este, -1 = el pasado). */
function ponerMes(offset) {
  const hoy = new Date();
  const ini = new Date(hoy.getFullYear(), hoy.getMonth() + offset, 1);
  const fin = new Date(hoy.getFullYear(), hoy.getMonth() + offset + 1, 0);
  ponerRango(isoDia(ini), isoDia(fin));
}

/* Lunes a domingo de la semana con `offset` (0 = esta, -1 = la pasada). Escribe en el rango
   COMPARTIDO, asi que el dashboard y la tabla de leads quedan mirando la misma semana: si
   cada pantalla tuviera su periodo, cuadrar los numeros entre ellas seria imposible. */
function ponerSemana(offset) {
  const [lunes, domingo] = semana(offset);
  ponerRango(isoDia(lunes), isoDia(domingo));
}

function semana(offset) {
  const hoy = new Date();
  const lunes = new Date(hoy.getFullYear(), hoy.getMonth(),
                         hoy.getDate() - ((hoy.getDay() + 6) % 7) + offset * 7);
  const domingo = new Date(lunes.getFullYear(), lunes.getMonth(), lunes.getDate() + 6);
  return [lunes, domingo];
}

function ponerRango(desde, hasta) {
  dateRange = { desde, hasta };
  $('d-desde').value = desde;
  $('d-hasta').value = hasta;
  $('d-mes').value = '';
  $('d-anio').value = '';
  cerrarPeriodo();
  recargarPorRango();
}

function recargarPorRango() {
  page = 0;
  renderRangeChip();
  const v = vistaActual();
  if (v === 'leads') loadLeads();
  else if (v === 'etiquetado') loadEtiquetado();
  else if (v === 'scoreboard') loadScoreboard();
  else loadDashboard();
  refreshTagBadge();   // la insignia lateral se ve desde cualquier vista
}

function hayRango() { return !!(dateRange.desde || dateRange.hasta); }

/* Texto del rango activo, para reutilizarlo en el chip y en los mensajes de lista vacia. */
function textoRango() {
  const d = dateRange.desde ? fmtDia(dateRange.desde) : '…';
  const h = dateRange.hasta ? fmtDia(dateRange.hasta) : '…';
  return d === h ? d : `${d} – ${h}`;
}

/* Un solo popover para las cuatro vistas; se ancla al chip que lo abrio. */
let periodoAncla = null;
function abrirPeriodo(btn) {
  const pop = $('periodo-pop');
  if (!pop.hidden && periodoAncla === btn) { cerrarPeriodo(); return; }
  cerrarTagPops();
  periodoAncla = btn;
  $('d-desde').value = dateRange.desde;
  $('d-hasta').value = dateRange.hasta;
  if (!aniosCargados) api('GET', '/stats').then(s => s && poblarAnios(s.primer_lead));
  pop.hidden = false;
  const r = btn.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight, M = 8;
  const left = Math.max(M, Math.min(r.left, window.innerWidth - w - M));
  const top = r.bottom + 6 + h <= window.innerHeight - M ? r.bottom + 6 : Math.max(M, r.top - 6 - h);
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
}
function cerrarPeriodo() {
  const pop = $('periodo-pop');
  if (pop) pop.hidden = true;
  periodoAncla = null;
}

/* Chip de periodo, el mismo en las cuatro vistas. Solo la tabla de leads tiene buscador,
   y por eso solo ella puede quedar «en pausa». */
function renderRangeChip() {
  document.querySelectorAll('.periodo-chip').forEach(chip => {
    const enLeads = !!chip.closest('#view-leads');
    // Buscar ignora el rango a proposito (ver list_leads en main.py). Si hay un rango puesto
    // se dice, en vez de dejar un chip azul prometiendo un filtro que no se esta aplicando.
    if (enLeads && textoBusqueda() && hayRango()) {
      chip.className = 'chip periodo-chip is-paused';
      chip.innerHTML = `<button type="button" class="chip-main" onclick="abrirPeriodo(this.parentNode)" title="Mientras buscas, el periodo no se aplica">${ico('calendar', 'ico-sm')}<span>Fechas en pausa mientras buscas</span></button>`;
      return;
    }
    chip.className = 'chip periodo-chip' + (hayRango() ? ' is-active' : '');
    chip.innerHTML = `<button type="button" class="chip-main" onclick="abrirPeriodo(this.parentNode)" aria-haspopup="dialog" title="Cambiar el periodo">${ico('calendar', 'ico-sm')}<span>${hayRango() ? esc(textoRango()) : 'Todo el histórico'}</span>${ico('chevron-down', 'ico-sm')}</button>`
      + (hayRango() ? `<button type="button" class="chip-x" onclick="limpiarRango()" title="Quitar el periodo" aria-label="Quitar el periodo">${ico('close', 'ico-sm')}</button>` : '');
  });
  pintarSemanaActiva();
}

/* Marca en el control segmentado del scoreboard la semana que coincide con el rango. */
function pintarSemanaActiva() {
  document.querySelectorAll('#sb-semanas button').forEach(b => {
    const [l, d] = semana(parseInt(b.dataset.semana, 10));
    b.classList.toggle('active', dateRange.desde === isoDia(l) && dateRange.hasta === isoDia(d));
  });
}

/* ══════════ Buscador (barra superior) ══════════ */

function textoBusqueda() { return $('q-inp').value.trim(); }

let resultados = { q: '', items: [], total: 0 };
let resultadoSel = -1;

/* Una peticion por tecla sobraria; 300 ms es donde ya se dejo de escribir pero todavia no
   se percibe espera. En la tabla de leads el texto filtra la tabla directamente (la tabla
   ES el resultado); desde cualquier otra vista se enseña un desplegable con los primeros. */
function onBuscar() {
  const q = textoBusqueda();
  $('q-clr').hidden = !q;
  $('search').classList.toggle('has-text', !!q);
  clearTimeout(buscarTimer);
  if (!q) {
    ocultarResultados();
    resultados = { q: '', items: [], total: 0 };
    if (vistaActual() === 'leads') { page = 0; loadLeads(); } else renderRangeChip();
    return;
  }
  buscarTimer = setTimeout(async () => {
    if (vistaActual() === 'leads') { page = 0; loadLeads(); return; }
    const data = await api('GET', '/leads?' + new URLSearchParams({ q, limit: 6, offset: 0 }));
    if (!data || textoBusqueda() !== q) return;   // llego tarde: ya se escribio otra cosa
    resultados = { q, items: data.items, total: data.total };
    resultadoSel = -1;
    renderResultados();
  }, 300);
}

function renderResultados() {
  const pop = $('search-results');
  const { q, items, total } = resultados;
  if (!q) { pop.hidden = true; return; }
  if (!items.length) {
    pop.innerHTML = `<div class="search-empty">Ningún lead coincide con «${esc(q)}»</div>`;
  } else {
    pop.innerHTML = items.map((l, i) => {
      const nombre = nombreLead(l);
      const sub = [l.company_name, l.email].filter(Boolean).join(' · ');
      return `<button type="button" class="search-item" role="option" id="sr-${i}" aria-selected="${i === resultadoSel}"
          onmousedown="event.preventDefault()" onclick="abrirResultado('${esc(l.lead_id)}')">
        ${avatar(nombre, 'avatar-sm', l.lead_id)}
        <span class="search-item-body"><span class="search-item-name">${nombre ? esc(nombre) : 'Sin nombre'}</span>
          ${sub ? `<span class="search-item-sub">${esc(sub)}</span>` : ''}</span>
        <code>+${esc(l.lead_id)}</code></button>`;
    }).join('') + `<div class="search-foot"><button type="button" onmousedown="event.preventDefault()" onclick="verTodosResultados()">
        Ver ${total > items.length ? `los ${total} resultados` : `${total === 1 ? 'el resultado' : 'los ' + total + ' resultados'}`} en la tabla</button></div>`;
  }
  pop.hidden = false;
}

function mostrarResultados() {
  if (resultados.q && resultados.q === textoBusqueda() && vistaActual() !== 'leads') renderResultados();
}
function ocultarResultados() {
  const pop = $('search-results');
  if (pop) pop.hidden = true;
}

function onBuscarTecla(e) {
  const pop = $('search-results');
  const abierto = !pop.hidden && resultados.items.length;
  if (e.key === 'Escape') { ocultarResultados(); e.target.blur(); return; }
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    if (!abierto) return;
    e.preventDefault();
    const n = resultados.items.length;
    resultadoSel = e.key === 'ArrowDown' ? (resultadoSel + 1) % n : (resultadoSel - 1 + n) % n;
    pop.querySelectorAll('.search-item').forEach((b, i) => b.setAttribute('aria-selected', i === resultadoSel));
    return;
  }
  if (e.key === 'Enter') {
    e.preventDefault();
    if (abierto && resultadoSel >= 0) abrirResultado(resultados.items[resultadoSel].lead_id);
    else if (textoBusqueda()) verTodosResultados();
  }
}

function abrirResultado(leadId) {
  ocultarResultados();
  nav('detail', leadId);
}

function verTodosResultados() {
  ocultarResultados();
  page = 0;
  if (vistaActual() !== 'leads') nav('leads'); else loadLeads();
}

function limpiarBusqueda() {
  $('q-inp').value = '';
  onBuscar();
  $('q-inp').focus();
}

/* Lleva el rango, igual que la lista del dashboard y la propia cola de etiquetado: las
   tres cuentan lo mismo y tienen que decir el mismo numero. Antes esta pedia /stats sin
   rango, asi que la insignia se quedaba fija mientras la tarjeta encogia con la fecha. */
async function refreshTagBadge() {
  const s = await api('GET', '/stats?' + aplicarRango(new URLSearchParams()));
  if (!s) return;
  const n = s.sin_etiquetas || 0;
  const b = $('tag-badge');
  b.hidden = !n;
  b.textContent = n;
}

/* ══════════ Leads ══════════ */
function resetAndLoad() { page = 0; loadLeads(); }

const FILTROS_EXTRA = [
  { id: 'f-plan',      label: 'Plan',        opts: PLAN_ESTADOS.map(p => [p.v, p.l]) },
  { id: 'f-seg',       label: 'Seguimiento', opts: SEG_FILTROS.map(s => [s.v, s.l]) },
  { id: 'f-qualified', label: 'Calificado',  opts: [['true', 'Sí'], ['false', 'No']] },
];

/* Chip de filtro con un <select> nativo encima, invisible: se ve el chip y se usa el
   desplegable del sistema, que ya funciona con teclado y en el celular. */
function filtroChip(id, label, opts, cls = '', todos = 'Todos') {
  return `<label class="filter ${cls}" data-label="${esc(label)}">
      <span class="filter-label">${esc(label)}</span><span class="filter-value">${esc(todos)}</span>${ico('chevron-down', 'ico-sm')}
      <select id="${id}" onchange="resetAndLoad()" aria-label="${esc(label)}">
        ${todos ? `<option value="">${esc(todos)}</option>` : ''}
        ${opts.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('')}
      </select></label>`;
}

/* Los filtros de etiqueta se generan desde TAG_GROUPS: uno por grupo, de seleccion unica.
   Combinarlos filtra por interseccion ("Demo agendada" Y "Diego"), que es como se leen. */
function renderTagFiltros() {
  $('tag-filtros').innerHTML =
    TAG_GROUPS.map(g => filtroChip(`f-tag-${g.key}`, g.label, g.tags.map(t => [t.v, t.l]))).join('')
    + FILTROS_EXTRA.map(f => filtroChip(f.id, f.label, f.opts)).join('')
    // Orden: en escritorio se cambia desde la cabecera de la tabla; este chip solo se ve
    // en el celular, donde la tabla no tiene cabecera.
    + filtroChip('f-sort', 'Ordenar', [['recientes', 'Recientes'], ['score', 'Prob. conversión']], 'filter-sort', '');
}

/* Repinta el texto de cada chip, cuales estan activos, el boton «Limpiar filtros» y, en el
   celular, la fila de filtros activos con su aspa. */
function actualizarEstadoFiltros() {
  let activos = 0;
  const chips = [];
  document.querySelectorAll('#tag-filtros .filter').forEach(f => {
    const sel = f.querySelector('select');
    const opt = sel.options[sel.selectedIndex];
    f.querySelector('.filter-value').textContent = opt ? opt.text : '';
    const esOrden = f.classList.contains('filter-sort');
    const on = esOrden ? sel.value !== 'recientes' : !!sel.value;
    f.classList.toggle('is-active', on);
    if (on && !esOrden) { activos++; chips.push({ id: sel.id, label: f.dataset.label, text: opt.text }); }
  });
  $('clear-filters').hidden = !activos;
  $('filters-btn-txt').textContent = activos ? `Filtros (${activos})` : 'Filtros';
  $('active-chips').innerHTML = chips.map(c => `<span class="chip is-active"><span class="chip-main">${esc(c.label)}: ${esc(c.text)}</span>
      <button type="button" class="chip-x" aria-label="Quitar filtro ${esc(c.label)}" onclick="quitarFiltro('${c.id}')">${ico('close', 'ico-sm')}</button></span>`).join('');
  pintarOrden();
  return activos;
}

function quitarFiltro(id) { $(id).value = ''; resetAndLoad(); }

function limpiarFiltrosLeads() {
  document.querySelectorAll('#tag-filtros select').forEach(s => { s.value = s.id === 'f-sort' ? 'recientes' : ''; });
  resetAndLoad();
}

/* Vacia buscador, filtros y periodo de una vez, con una sola recarga. */
function limpiarTodoLeads() {
  $('q-inp').value = '';
  $('q-clr').hidden = true;
  $('search').classList.remove('has-text');
  document.querySelectorAll('#tag-filtros select').forEach(s => { s.value = s.id === 'f-sort' ? 'recientes' : ''; });
  if (hayRango()) limpiarRango(); else resetAndLoad();
}

function abrirFiltros() { $('filters').classList.add('open'); }
function cerrarFiltros() { const f = $('filters'); if (f) f.classList.remove('open'); }

function tagsFiltro() {
  return TAG_GROUPS.map(g => $(`f-tag-${g.key}`).value).filter(Boolean);
}

/* Los filtros que la barra tiene puestos ahora mismo, sin paginacion.
   Los usan la tabla y la exportacion: el CSV solo sirve si trae lo que se ve en pantalla,
   y eso deja de ser cierto en cuanto cada uno arma su propia query. */
function paramsLeads() {
  const params = new URLSearchParams();
  const tags = tagsFiltro();
  const qual = $('f-qualified').value;
  const sort = $('f-sort').value;
  const plan = $('f-plan').value;
  const seg  = $('f-seg').value;
  const q    = textoBusqueda();
  if (tags.length) params.set('tags', tags.join(','));
  if (plan)    params.set('plan_estado', plan);
  if (qual)    params.set('qualified', qual);
  if (sort)    params.set('sort', sort);
  if (seg)     params.set('seguimiento', seg);
  if (q)       params.set('q', q);
  // Mientras se busca no se manda el rango: el backend tampoco lo aplicaria (buscar un
  // telefono tiene que encontrarlo sea de la fecha que sea).
  if (!q) aplicarRango(params);
  return params;
}

/* Orden desde la cabecera de la tabla. Escribe en el mismo <select> que usa el celular,
   asi paramsLeads() sigue teniendo una sola fuente. */
function ordenarPor(sort) {
  $('f-sort').value = sort;
  resetAndLoad();
}
function pintarOrden() {
  const s = $('f-sort').value;
  document.querySelectorAll('.leads-table th.sortable').forEach(th => {
    const on = th.dataset.sort === s;
    th.setAttribute('aria-sort', on ? 'descending' : 'none');
    th.querySelector('use').setAttribute('href', on ? '#i-sort-desc' : '#i-sort');
  });
}

async function exportarCSV(btn) {
  const lbl = btn.querySelector('span') || btn;
  const etiqueta = lbl.textContent;
  btn.disabled = true;
  lbl.textContent = 'Preparando…';
  try {
    // No pasa por api(): esa hace res.json() y aqui llega un fichero. Y tampoco puede ser
    // un <a href> normal, porque una descarga del navegador no lleva la cabecera
    // Authorization y el endpoint devolveria 401.
    const res = await fetch('/api/leads/export.csv?' + paramsLeads(),
                            { headers: { 'Authorization': 'Bearer ' + token() } });
    if (res.status === 401) { sesionExpirada(); return; }
    if (!res.ok) throw new Error('el servidor respondió ' + res.status);

    // El nombre lo pone el servidor: lleva la fecha en hora de Lima. Calcularla aqui con
    // toISOString daria la de UTC, y a partir de las 19:00 el fichero se llamaria mañana.
    const cd = res.headers.get('Content-Disposition') || '';
    const encontrado = cd.match(/filename="([^"]+)"/);

    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement('a');
    a.href = url;
    a.download = encontrado ? encontrado[1] : 'leads.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    toast('No se pudo exportar: ' + e.message, 'crit', 6000);
  } finally {
    btn.disabled = false;
    lbl.textContent = etiqueta;
  }
}

/* Cada carga lleva su numero: si se cambian dos filtros seguidos, la respuesta lenta del
   primero no puede pisar la del segundo. */
let leadsReq = 0;

async function loadLeads() {
  const params = paramsLeads();
  params.set('limit', PAGE);
  params.set('offset', page * PAGE);

  actualizarEstadoFiltros();
  renderRangeChip();
  const card = $('table-card');
  const tb = $('tbody');
  // Esqueleto solo la primera vez; si ya hay filas, se atenuan hasta que llegue la nueva
  // pagina, que es menos brusco que vaciar la tabla.
  if (!tb.querySelector('tr[data-id]')) tb.innerHTML = skeletonRows(6);
  else card.classList.add('is-loading');

  const req = ++leadsReq;
  const data = await api('GET', '/leads?' + params);
  if (req !== leadsReq) return;
  card.classList.remove('is-loading');
  if (!data) return;

  $('count-badge').textContent = `${data.total} lead${data.total !== 1 ? 's' : ''}`;
  renderRows(data.items);
  renderPages(data.total);
}

function skeletonRows(n) {
  const anchos = [70, 40, 55, 60, 65, 50, 45, 30];
  return Array(n).fill(0).map(() => `<tr class="sk-row">${anchos.map((w, i) => `<td${i === 0 ? ' class="col-lead"' : ''}><span class="skeleton sk-line" style="width:${w}%"></span></td>`).join('')}</tr>`).join('');
}

function probCell(l) {
  const prob = (l.conversion_prob != null) ? Math.round(l.conversion_prob * 100) : null;
  if (prob != null) return scoreRing(prob, 32);
  if (l.has_transcript) {
    return `<button type="button" class="btn btn-secondary btn-sm" onclick="event.stopPropagation();scoreLead('${esc(l.lead_id)}', this)">Calcular</button>`;
  }
  return `<span class="cell-muted" title="Sin conversación suficiente para puntuar">—</span>`;
}

function estadoCell(l) {
  return `<div class="estado-cell">
      ${l.tipo_lead ? `<span class="badge badge-blue">${esc(l.tipo_lead)}</span>` : '<span class="cell-muted">—</span>'}
      <span class="estado-q${l.qualified ? ' si' : ''}">${l.qualified ? ico('check') + 'Calificado' : 'Sin calificar'}</span>
    </div>`;
}

/* Celda de seguimiento: la accion y cuanto le queda. El color es el que hace que un
   vencido salte a la vista sin tener que leer la fecha. */
function segCell(l) {
  if (!l.siguiente_paso) return '<span class="cell-muted">—</span>';
  const d = l.siguiente_paso_fecha != null ? diasDesde(l.siguiente_paso_fecha) : null;
  let cls = 'badge-gray', txt = 'sin fecha';
  if (d != null) {
    if (d > 0)       { cls = 'badge-red';   txt = `venció hace ${d} día${d !== 1 ? 's' : ''}`; }
    else if (d === 0) { cls = 'badge-amber'; txt = 'vence hoy'; }
    else              { cls = 'badge-gray';  txt = fmtDia(l.siguiente_paso_fecha); }
  }
  return `<div class="cell-sp">
      <div class="t" title="${esc(l.siguiente_paso)}">${esc(l.siguiente_paso)}</div>
      <div class="f"><span class="badge with-dot ${cls}">${esc(txt)}</span></div>
    </div>`;
}

function emptyLeads() {
  const q = textoBusqueda();
  const hayFiltros = document.querySelectorAll('#tag-filtros .filter.is-active:not(.filter-sort)').length > 0;
  const frase = q
    ? `Ningún lead coincide con «${esc(q)}».`
    : hayRango() ? `Sin leads entre ${esc(textoRango())}${hayFiltros ? ' con estos filtros' : ''}.`
    : hayFiltros ? 'Sin leads con estos filtros.' : 'Todavía no hay leads.';
  return `<div class="empty">${ico('inbox')}<p>${frase}</p>
    ${(q || hayRango() || hayFiltros) ? '<button type="button" class="btn btn-secondary btn-sm" onclick="limpiarTodoLeads()">Ver todos los leads</button>' : ''}</div>`;
}

function renderRows(leads) {
  const tb = $('tbody');
  if (!leads.length) {
    tb.innerHTML = `<tr class="empty-row"><td colspan="8">${emptyLeads()}</td></tr>`;
    return;
  }
  tb.innerHTML = leads.map(l => {
    const id = esc(l.lead_id);
    const nombre = nombreLead(l);
    const sub = [l.company_name, l.email].filter(Boolean).map(esc).join(' · ');
    return `<tr data-id="${id}" tabindex="0" onclick="nav('detail','${id}')"
                onkeydown="if(event.key==='Enter'&&event.target===this)nav('detail','${id}')">
      <td class="col-lead"><div class="cell-lead">${avatar(nombre, '', l.lead_id)}
        <div class="cell-lead-body">
          <div class="lead-name${nombre ? '' : ' is-empty'}">${nombre ? esc(nombre) : 'Sin nombre'}</div>
          ${sub ? `<div class="lead-sub" title="${sub}">${sub}</div>` : ''}
          <div class="lead-tel">+${id}</div>
        </div></div></td>
      <td data-label="Score">${probCell(l)}</td>
      <td data-label="Estado">${estadoCell(l)}</td>
      <td data-label="Plan">${planCell(l)}</td>
      <td data-label="Etiquetas" onclick="event.stopPropagation()">
        <div class="tag-cell" id="tc-${id}">
          <span class="tag-badges">${tagBadges(tagsOf(l))}</span>
          <button type="button" class="btn-tag-add" title="Editar etiquetas" aria-label="Editar etiquetas" onclick="toggleTagPop('${id}', this)">${ico('plus')}</button>
        </div>
      </td>
      <td data-label="Seguimiento">${segCell(l)}</td>
      <td data-label="Fecha" class="col-fecha cell-muted" title="${esc(fmtFechaHora(l.captured_at))}">${fmtDate(l.captured_at)}</td>
      <td class="col-acciones"><div class="row-actions">
        <button type="button" class="btn-icon" title="Brief de venta" aria-label="Brief de venta" onclick="event.stopPropagation();showBrief('${id}')">${ico('file')}<span class="btn-txt">Brief</span></button>
        <button type="button" class="btn-icon" title="Abrir el lead" aria-label="Abrir el lead" onclick="event.stopPropagation();nav('detail','${id}')">${ico('chevron-right')}<span class="btn-txt">Abrir</span></button>
      </div></td>
    </tr>`;
  }).join('');
}

/* ══════════ Etiquetas ══════════ */
/* Etiquetas vigentes por lead_id. Lo alimentan las tres vistas que muestran un picker
   (tabla, detalle, etiquetado) para que el toggle sepa de que conjunto parte sin pedir
   otra vez el lead al servidor. */
const tagsCache = {};

function tagsOf(lead) {
  // outcome_tags es text[] en Postgres y asyncpg lo entrega como lista, pero un lead
  // servido desde una version anterior podria llegar sin la clave.
  const tags = Array.isArray(lead.outcome_tags) ? lead.outcome_tags : [];
  tagsCache[lead.lead_id] = tags;
  return tags;
}

function sortTags(tags) {
  return TAG_ORDER.filter(v => tags.includes(v));
}

/* Pill del estado comercial. Debajo va la fecha de vencimiento, que es la que da el
   matiz: "Ex-cliente" no dice lo mismo si se fue hace un mes que hace dos años. */
function planCell(l) {
  if (!l.plan_estado) return '<span class="cell-muted" title="Aún no se ha sincronizado">—</span>';
  const cls = PLAN_CLASS[l.plan_estado] || 'gray';
  const txt = PLAN_LABEL[l.plan_estado] || l.plan_estado;
  const pie = l.plan_estado === 'sin_cuenta'
    ? ''
    : `<div class="plan-sub">${l.plan_expira ? 'vence ' + fmtDate(l.plan_expira) : ''}` +
      `${l.plan_pagos ? ` · ${l.plan_pagos} pago${l.plan_pagos !== 1 ? 's' : ''}` : ''}</div>`;
  return `<span class="badge with-dot badge-${cls}" title="${esc(l.plan_nombre || '')}">${esc(txt)}</span>${pie}`;
}

function tagBadges(tags) {
  const orden = sortTags(tags);
  if (!orden.length) return '<span class="cell-muted">—</span>';
  return orden.map(v => `<span class="badge badge-${TAG_CLASS[v]}">${esc(TAG_LABEL[v])}</span>`).join(' ');
}

/* Botones toggle agrupados por categoria. ctx dice que refrescar despues de guardar. */
function tagPicker(leadId, tags, ctx) {
  const id = esc(leadId);
  return `<div class="tag-picker">` + TAG_GROUPS.map(g => `
    <div class="tag-group-title">${g.label}</div>
    <div class="tag-btns">${g.tags.map(t => {
      const sel = tags.includes(t.v) ? ` sel-${t.c}` : '';
      return `<button type="button" class="btn-tag${sel}" data-tag="${t.v}" aria-pressed="${tags.includes(t.v)}" onclick="toggleTag('${id}','${t.v}','${ctx}',this)">${esc(t.l)}</button>`;
    }).join('')}</div>`).join('') + `</div>`;
}

/* Alterna una etiqueta. El estado de partida sale de tagsCache y el servidor devuelve el
   conjunto final ya normalizado: ese es el que se pinta. */
async function toggleTag(leadId, tag, ctx, btn) {
  const actuales = tagsCache[leadId] || [];
  const nuevas = actuales.includes(tag) ? actuales.filter(t => t !== tag) : [...actuales, tag];
  if (btn) btn.disabled = true;
  await saveTags(leadId, nuevas, ctx);
  if (btn) btn.disabled = false;
}

async function saveTags(leadId, tags, ctx) {
  const res = await api('PATCH', `/leads/${leadId}/outcome`, { tags: sortTags(tags) });
  if (!res || !res.ok) {
    if (res && res.detail) toast('No se pudo guardar la etiqueta: ' + res.detail, 'crit');
    return;   // sin confirmacion del servidor no se refresca nada
  }
  tagsCache[leadId] = res.tags;
  refreshTagBadge();
  // Tabla y cola de etiquetado se repintan en sitio en vez de recargarse: lo normal es
  // marcar varias etiquetas seguidas, y recargar cerraria el popover (tabla) o sacaria al
  // lead de la cola (etiquetado, que lista justo los que no tienen ninguna). La cola se
  // refresca de verdad al elegir otro lead. El detalle si se recarga: no desaparece.
  if (ctx === 'leads') {
    const celda = $(`tc-${leadId}`);
    if (celda) celda.querySelector('.tag-badges').innerHTML = tagBadges(res.tags);
    // El picker cuelga del <body>, no de la celda, y solo hay uno abierto: el de este lead.
    repintarBotonesTag(document.querySelector('.tag-pop'), res.tags);
  }
  if (ctx === 'tag') {
    repintarBotonesTag($('tag-panel'), res.tags);
    // Ya hay un resultado marcado: el siguiente paso natural es pasar al siguiente lead.
    const next = $('tag-next');
    if (next && res.tags.some(t => TAG_GROUP[t] === 'estado')) next.classList.replace('btn-secondary', 'btn-primary');
  }
  if (ctx === 'detail') loadDetail(leadId);
}

/* Reaplica el estado seleccionado a los botones de un picker ya renderizado. */
function repintarBotonesTag(root, tags) {
  if (!root) return;
  root.querySelectorAll('.btn-tag').forEach(b => {
    const v = b.dataset.tag;
    b.className = 'btn-tag' + (tags.includes(v) ? ` sel-${TAG_CLASS[v]}` : '');
    b.setAttribute('aria-pressed', tags.includes(v));
  });
}

/* Popover de la celda de la tabla. Solo puede haber uno abierto a la vez; el boton que lo
   abrio queda marcado para que un segundo clic lo cierre en vez de reabrirlo. En el celular
   se convierte en hoja inferior (lo decide el CSS; aqui solo se le pone cabecera). */
const POP_W = 320, POP_MARGIN = 8;
function toggleTagPop(leadId, btn) {
  const yaAbierto = btn.dataset.pop === '1';
  cerrarTagPops();
  cerrarPeriodo();
  if (yaAbierto) return;

  const hoja = window.matchMedia('(max-width: 599px)').matches;
  const pop = document.createElement('div');
  pop.className = 'pop tag-pop';
  pop.innerHTML = (hoja
    ? `<div class="sheet-head"><span class="sheet-title">Etiquetas</span><button type="button" class="btn-icon" onclick="cerrarTagPops()" aria-label="Cerrar">${ico('close')}</button></div>`
    : '') + tagPicker(leadId, tagsCache[leadId] || [], 'leads');
  pop.onclick = e => e.stopPropagation();   // los clics del picker no deben cerrarlo
  document.body.appendChild(pop);

  if (!hoja) {
    // Se ancla al boton y se mete dentro de la ventana; si no cabe abajo, se abre arriba.
    const r = btn.getBoundingClientRect();
    const alto = pop.offsetHeight;
    const cabeAbajo = r.bottom + POP_MARGIN + alto <= window.innerHeight;
    pop.style.left = `${Math.max(POP_MARGIN, Math.min(r.left, window.innerWidth - POP_W - POP_MARGIN))}px`;
    pop.style.top = `${cabeAbajo ? r.bottom + POP_MARGIN : Math.max(POP_MARGIN, r.top - POP_MARGIN - alto)}px`;
  }
  btn.dataset.pop = '1';
}

function cerrarTagPops() {
  document.querySelectorAll('.tag-pop').forEach(p => p.remove());
  document.querySelectorAll('.btn-tag-add[data-pop]').forEach(b => delete b.dataset.pop);
}

function renderPages(total) {
  const pages = Math.ceil(total / PAGE);
  const el = $('pagination');
  if (pages <= 1) { el.innerHTML = ''; return; }
  const ini = page * PAGE + 1, fin = Math.min(total, (page + 1) * PAGE);
  let h = `<button type="button" class="btn-page" onclick="goPage(${page - 1})" ${page === 0 ? 'disabled' : ''} aria-label="Página anterior">${ico('arrow-left', 'ico-sm')}</button>`;
  const start = Math.max(0, page - 2), end = Math.min(pages, start + 5);
  for (let i = start; i < end; i++)
    h += `<button type="button" class="btn-page${i === page ? ' active' : ''}" onclick="goPage(${i})" ${i === page ? 'aria-current="page"' : ''}>${i + 1}</button>`;
  h += `<button type="button" class="btn-page" onclick="goPage(${page + 1})" ${page >= pages - 1 ? 'disabled' : ''} aria-label="Página siguiente">${ico('arrow-right', 'ico-sm')}</button>`;
  el.innerHTML = `<span class="count">${ini}–${fin} de ${total}</span><div class="pages">${h}</div>`;
}

function goPage(p) {
  page = p;
  loadLeads();
  $('table-wrap').scrollTop = 0;
  $('main').scrollTop = 0;
  window.scrollTo(0, 0);
}

async function scoreLead(leadId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  const data = await api('POST', `/leads/${leadId}/score`);
  if (data) loadLeads();
  else if (btn) { btn.disabled = false; btn.textContent = 'Calcular'; }
}

/* ══════════ Detalle de lead ══════════ */
function parseTranscript(t) {
  // Formato esperado: lineas "role: texto" (human/ai/agent). Si no matchea, texto plano.
  const lines = (t || '').split('\n');
  const msgs = [];
  let cur = null;
  for (const ln of lines) {
    const m = ln.match(/^(human|ai|agente|agent|lead|bot|user|assistant)\s*:\s*(.*)$/i);
    if (m) {
      if (cur) msgs.push(cur);
      const role = /^(human|lead|user)$/i.test(m[1]) ? 'human' : 'ai';
      cur = { role, tag: m[1].toUpperCase(), text: m[2] };
    } else if (cur) {
      cur.text += '\n' + ln;
    }
  }
  if (cur) msgs.push(cur);
  return msgs;
}

function renderTranscript(t) {
  if (!t) return `<div class="empty">${ico('message')}<p>Sin conversación disponible.</p></div>`;
  const msgs = parseTranscript(t);
  if (!msgs.length) return `<div class="transcript"><div class="msg msg-ai">${esc(t)}</div></div>`;
  return `<div class="transcript">${msgs.map(m =>
    `<div class="msg msg-${m.role}"><div class="msg-tag">${esc(m.tag)}</div>${esc(m.text.trim())}</div>`
  ).join('')}</div>`;
}

const DETAIL_FIELDS = [
  ['contact_name', 'Nombre'], ['company_name', 'Empresa'], ['email', 'Email'],
  ['tax_id', 'RUC'], ['wa_display_name', 'Nombre WhatsApp'], ['segmento', 'Segmento'],
  ['industry', 'Industria'], ['num_rucs', 'N° RUCs'], ['volumen_comprobantes', 'Comprobantes/mes'],
  ['solucion_actual', 'Solución actual'], ['dolor_principal', 'Dolor principal'],
  ['objecion', 'Objeción'], ['urgencia', 'Urgencia'], ['tipo_lead', 'Tipo de lead'],
  ['ticket_estimado', 'Ticket estimado'], ['message_count', 'N° mensajes'],
];

async function copiarTexto(texto, aviso) {
  try {
    await navigator.clipboard.writeText(texto);
    toast(aviso || 'Copiado', 'ok', 2500);
  } catch (e) {
    $('copiar-texto').value = texto;
    abrirModal('copiar-overlay');
    $('copiar-texto').select();
  }
}

async function loadDetail(leadId) {
  if (!leadId) { nav('leads'); return; }
  currentLeadId = leadId;
  const el = $('detail-content');
  el.innerHTML = '<div class="loading"><span class="spinner"></span>Cargando…</div>';
  const [l, notas, hitos] = await Promise.all([
    api('GET', `/leads/${leadId}`),
    api('GET', `/leads/${leadId}/notas`),
    api('GET', `/leads/${leadId}/eventos`),
  ]);
  if (!l) return;
  if (l.detail) { el.innerHTML = `<div class="empty">${ico('alert')}<p>${esc(l.detail)}</p><a class="btn btn-secondary btn-sm" href="#leads">Volver a leads</a></div>`; return; }

  const id = esc(l.lead_id);
  const nombre = nombreLead(l);
  const prob = (l.conversion_prob != null) ? Math.round(l.conversion_prob * 100) : null;
  const modulos = (l.modulos_interes || []).map(m => `<span class="badge badge-blue">${esc(m)}</span>`).join(' ') || '—';
  const fields = DETAIL_FIELDS
    .filter(([k]) => l[k] != null && l[k] !== '')
    .map(([k, label]) => `<div class="field"><b>${label}</b><span>${esc(l[k])}</span></div>`)
    .join('');

  el.innerHTML = `
    <div class="crumbs"><a href="#leads">Leads</a>${ico('chevron-right')}<span>${esc(nombre || '+' + l.lead_id)}</span></div>
    <div class="card detail-head">
      <div class="detail-id">${avatar(nombre, 'avatar-lg', l.lead_id)}
        <div>
          <h2>${nombre ? esc(nombre) : 'Sin nombre'}</h2>
          <div class="detail-meta">
            <code>+${id}</code>
            <button type="button" class="btn-icon btn-xs" title="Copiar teléfono" aria-label="Copiar teléfono" onclick="copiarTexto('+${id}', 'Teléfono copiado')">${ico('copy', 'ico-sm')}</button>
            ${l.company_name ? `<span>· ${esc(l.company_name)}</span>` : ''}
            <span>· capturado ${fmtDate(l.captured_at)}</span>
          </div>
          <div class="detail-badges">
            ${l.qualified ? '<span class="badge badge-green">Calificado</span>' : '<span class="badge badge-gray">No calificado</span>'}
            ${tagBadges(tagsOf(l)).replace('<span class="cell-muted">—</span>', '')}
          </div>
        </div>
      </div>
      <div class="detail-score">${prob != null
        ? scoreRing(prob, 64) + '<span class="hint">probabilidad de conversión</span>'
        : (l.transcript
          ? `<button type="button" class="btn btn-secondary btn-sm" onclick="scoreDetail('${id}', this)">Calcular score</button>`
          : '<span class="hint">Sin conversación suficiente para puntuar</span>')}</div>
      <div class="detail-actions">
        <button type="button" class="btn btn-primary btn-sm" onclick="showBrief('${id}')">${ico('file', 'ico-sm')}Generar brief</button>
      </div>
    </div>
    <div class="detail-grid">
      <div class="detail-col">
        <div class="panel panel-first">
          <div class="panel-title">Siguiente paso</div>
          ${siguientePasoForm(l)}
        </div>
        <div class="panel panel-first">
          <div class="panel-title">Etiquetas</div>
          ${tagPicker(l.lead_id, tagsOf(l), 'detail')}
        </div>
        <div class="panel">
          <div class="panel-title">Plan en MAU Comunica</div>
          ${planPanel(l)}
        </div>
        <div class="panel">
          <div class="panel-title">Fechas del embudo</div>
          ${hitosPanel(l.lead_id, hitos)}
        </div>
        <div class="panel">
          <div class="panel-title">Notas</div>
          ${notasPanel(l.lead_id, notas)}
        </div>
        <div class="panel">
          <div class="panel-title">Datos del lead</div>
          ${fields || '<span class="cell-muted">Sin datos extraídos.</span>'}
          <div class="field"><b>Módulos de interés</b><span>${modulos}</span></div>
        </div>
      </div>
      <div class="panel conv-panel">
        <div class="panel-title">Conversación${l.message_count ? `<span class="hint">${l.message_count} mensajes</span>` : ''}</div>
        ${renderTranscript(l.transcript)}
      </div>
    </div>`;
}

/* ══════════ Siguiente paso ══════════ */
/* Campo corto y aparte de las notas a proposito: la nota explica POR QUE el lead esta donde
   esta, y esto es QUE toca hacer y CUANDO. Solo el segundo puede vencer, y por eso es el
   unico que ataca el seguimiento que se cae. */
function siguientePasoForm(l) {
  const d = l.siguiente_paso_fecha != null ? diasDesde(l.siguiente_paso_fecha) : null;
  let estado = '';
  if (l.siguiente_paso && d != null) {
    estado = d > 0
      ? `<span class="badge with-dot badge-red">Vencido hace ${d} día${d !== 1 ? 's' : ''}</span>`
      : d === 0
        ? '<span class="badge with-dot badge-amber">Vence hoy</span>'
        : `<span class="badge with-dot badge-gray">Faltan ${-d} día${d !== -1 ? 's' : ''}</span>`;
  }
  const firma = l.siguiente_paso_autor
    ? `<div class="hint">Definido por <b>${esc(cap(l.siguiente_paso_autor))}</b> el ${esc(fmtFechaHora(l.siguiente_paso_at))}</div>`
    : '';
  const id = esc(l.lead_id);
  return `<div class="sp-form">
      <input type="text" id="sp-texto" maxlength="140" value="${esc(l.siguiente_paso || '')}"
             placeholder="Ej.: llamar para cerrar la cotización" aria-label="Siguiente paso">
      <div class="row">
        <input type="date" id="sp-fecha" value="${esc(l.siguiente_paso_fecha || '')}" aria-label="Fecha del siguiente paso">
        <button type="button" class="btn btn-primary btn-sm" onclick="guardarSiguientePaso('${id}', this)">Guardar</button>
        ${l.siguiente_paso ? `<button type="button" class="btn btn-ghost btn-sm" onclick="limpiarSiguientePaso('${id}', this)">Quitar</button>` : ''}
      </div>
      <div class="field-error" id="sp-err" hidden></div>
      ${estado ? `<div>${estado}</div>` : ''}${firma}
      <div class="hint">Una acción concreta con fecha, no un resumen. El porqué va en las notas.</div>
    </div>`;
}

async function guardarSiguientePaso(leadId, btn) {
  const texto = $('sp-texto').value.trim();
  const fecha = $('sp-fecha').value;
  const err = $('sp-err');
  if (texto && !fecha) {
    err.innerHTML = `${ico('alert', 'ico-sm')}Ponle fecha: sin fecha el siguiente paso no puede vencer y no aparecerá entre los vencidos.`;
    err.hidden = false;
    $('sp-fecha').focus();
    return;
  }
  err.hidden = true;
  btn.disabled = true; btn.textContent = 'Guardando…';
  const r = await api('PATCH', `/leads/${leadId}/siguiente-paso`, { texto, fecha: fecha || null });
  if (r && r.ok) { toast('Siguiente paso guardado', 'ok', 2500); loadDetail(leadId); return; }
  btn.disabled = false; btn.textContent = 'Guardar';
  if (r && r.detail) toast(r.detail, 'crit');
}

async function limpiarSiguientePaso(leadId, btn) {
  btn.disabled = true; btn.textContent = 'Quitando…';
  const r = await api('PATCH', `/leads/${leadId}/siguiente-paso`, { texto: '', fecha: null });
  if (r && r.ok) { loadDetail(leadId); return; }
  btn.disabled = false; btn.textContent = 'Quitar';
  if (r && r.detail) toast(r.detail, 'crit');
}

/* ══════════ Fechas del embudo ══════════ */

/* Cada etiqueta de estado deja un hito fechado, y de ahi salen los numeros del Scoreboard.
   La fecha se pone sola en el dia que se marca la etiqueta, que es lo correcto casi siempre.
   Esto existe para el caso que no: quien etiqueta el viernes lo de toda la semana mandaria
   todo al viernes, y la demo del martes contaria en la semana equivocada. */
function hitosPanel(leadId, hitos) {
  const items = (hitos && hitos.eventos) || [];
  if (!items.length) {
    return '<span class="hint">Sin hitos todavía. Marca una etiqueta de estado y se fechará sola.</span>';
  }
  const id = esc(leadId);
  return `<div class="hito-list">${items.map(h => `
      <div class="hito">
        <span class="badge badge-${TAG_CLASS[h.evento] || 'gray'}">${esc(TAG_LABEL[h.evento] || h.evento)}</span>
        <input type="date" id="hito-${esc(h.evento)}" value="${esc(h.fecha || '')}" aria-label="Fecha de ${esc(TAG_LABEL[h.evento] || h.evento)}"
               onchange="guardarHito('${id}', '${esc(h.evento)}', this)">
        ${h.autor === 'migracion'
          ? '<span class="hint">fecha estimada</span>'
          : `<span class="hint">${esc(cap(h.autor))}</span>`}
      </div>`).join('')}</div>
    <div class="hint" style="margin-top:8px">El Scoreboard cuenta cada hito en la semana de su fecha. Corrígela si la marcaste con retraso.</div>`;
}

async function guardarHito(leadId, evento, input) {
  if (!input.value) return;               // vaciar el campo no es una fecha: no se manda nada
  input.disabled = true;
  const r = await api('PATCH', `/leads/${leadId}/eventos/${evento}`, { fecha: input.value });
  input.disabled = false;
  if (!r || r.detail) {
    toast('No se pudo mover la fecha: ' + ((r && r.detail) || 'error de conexión'), 'crit');
    loadDetail(leadId);                   // se recarga para no dejar en pantalla un valor falso
  } else {
    toast('Fecha del hito actualizada', 'ok', 2500);
  }
}

/* ══════════ Notas ══════════ */
function notasPanel(leadId, notas) {
  const items = (notas && notas.items) || [];
  const id = esc(leadId);
  return `<div class="nota-form">
      <textarea id="nota-texto" maxlength="2000" aria-label="Nueva nota"
        placeholder="Por qué está donde está: qué pasó en la última conversación, qué dijo, qué lo frena."></textarea>
      <div class="row" style="margin-top:8px">
        <button type="button" class="btn btn-primary btn-sm" onclick="guardarNota('${id}', this)">Añadir nota</button>
        <span class="hint">Se firma sola con tu usuario y la fecha.</span>
      </div>
    </div>
    <div class="nota-list">${items.length ? items.map(n => `
      <div class="nota">
        <div class="nota-meta"><b>${esc(cap(n.autor))}</b> · ${esc(fmtFechaHora(n.creado_at))}</div>
        <div class="nota-texto">${esc(n.texto)}</div>
      </div>`).join('') : '<span class="hint">Todavía no hay notas en este lead.</span>'}</div>`;
}

async function guardarNota(leadId, btn) {
  const ta = $('nota-texto');
  const texto = ta.value.trim();
  if (!texto) { ta.focus(); return; }
  btn.disabled = true; btn.textContent = 'Guardando…';
  const r = await api('POST', `/leads/${leadId}/notas`, { texto });
  if (r && r.id) { loadDetail(leadId); return; }   // se repinta con la nota ya firmada
  btn.disabled = false; btn.textContent = 'Añadir nota';
  if (r && r.detail) toast(r.detail, 'crit');
}

/* Ficha del plan en el detalle. Es solo lectura: lo escribe el sync, no el vendedor. */
function planPanel(l) {
  if (!l.plan_estado) {
    return '<span class="hint">Sin sincronizar. Usa «Sincronizar planes» en el dashboard.</span>';
  }
  if (l.plan_estado === 'sin_cuenta') {
    return '<span class="hint">Este lead no tiene cuenta en MAU Comunica (no cruzó por teléfono ni por correo).</span>';
  }
  const filas = [
    ['Estado', `<span class="badge with-dot badge-${PLAN_CLASS[l.plan_estado] || 'gray'}">${esc(PLAN_LABEL[l.plan_estado] || l.plan_estado)}</span>`],
    ['Plan', l.plan_nombre ? esc(l.plan_nombre) : '—'],
    ['Inicio', l.plan_inicia ? fmtDate(l.plan_inicia) : '—'],
    ['Vencimiento', l.plan_expira ? fmtDate(l.plan_expira) : '—'],
    ['Pagos aprobados', l.plan_pagos != null ? String(l.plan_pagos) : '—'],
    ['Cruzado por', l.plan_match === 'correo' ? 'correo' : 'teléfono'],
    ['Sincronizado', l.plan_sync_at ? fmtDate(l.plan_sync_at) : '—'],
  ];
  return filas.map(([k, v]) => `<div class="field"><b>${k}</b><span>${v}</span></div>`).join('');
}

async function scoreDetail(leadId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Calculando…'; }
  const data = await api('POST', `/leads/${leadId}/score`);
  if (data && !data.detail) loadDetail(leadId);
  else {
    if (btn) { btn.disabled = false; btn.textContent = 'Calcular score'; }
    if (data && data.detail) toast(data.detail, 'crit');
  }
}

/* ══════════ Scoreboard ══════════ */
let scoreboardActual = null;

async function loadScoreboard() {
  const el = $('sb-body');
  if (!el.children.length) el.innerHTML = '<div class="loading"><span class="spinner"></span>Cargando…</div>';
  const s = await api('GET', '/scoreboard?' + aplicarRango(new URLSearchParams()));
  if (!s) return;
  scoreboardActual = s;

  const filas = [['Leads nuevos', s.leads_nuevos, 0]].concat(
    s.orden.map(v => [TAG_LABEL[v] || v, (s.por_evento || {})[v] || 0,
                      (s.aproximados || {})[v] || 0]));
  const aprox = Object.values(s.aproximados || {}).reduce((a, b) => a + b, 0);

  el.innerHTML = `
    ${hayRango() ? '' : `<div class="notice" style="margin-bottom:14px">${ico('info')}<div class="notice-body">Sin semana elegida se están contando todos los hitos del histórico. Pulsa <b>Esta semana</b> o <b>Semana pasada</b>.</div></div>`}
    <div class="card"><table class="table-plain">
      <thead><tr><th>Concepto</th><th class="num">Cantidad</th></tr></thead>
      <tbody>${filas.map(([l, n, a]) => `
        <tr><td>${esc(l)}</td><td class="num"><b>${n}</b>${
          a ? ` <span class="hint">(${a} aprox.)</span>` : ''}</td></tr>`).join('')}
      </tbody>
    </table></div>
    ${aprox ? `<div class="hint" style="margin-top:10px">${aprox} hito${aprox !== 1 ? 's' : ''} vienen del etiquetado anterior a este registro: su fecha es una estimación, no el día en que ocurrió.</div>` : ''}`;
}

/* Esto es lo que evita contar a mano: el Scoreboard sale ya en texto para pegar donde se
   lleve. Si el navegador no da permiso de portapapeles, se muestra para poder copiarlo. */
async function copiarScoreboard() {
  const s = scoreboardActual;
  if (!s) return;
  const lineas = [`Scoreboard ${s.desde || '(todo)'} a ${s.hasta || '(todo)'}`,
                  `Leads nuevos: ${s.leads_nuevos}`].concat(
    s.orden.map(v => `${TAG_LABEL[v] || v}: ${(s.por_evento || {})[v] || 0}`));
  copiarTexto(lineas.join('\n'), 'Scoreboard copiado');
}

/* ══════════ Etiquetado ══════════ */
async function loadEtiquetado() {
  const listEl = $('tag-list');
  if (!listEl.children.length) listEl.innerHTML = '<div class="loading"><span class="spinner"></span>Cargando…</div>';
  // Sin ninguna etiqueta, no outcome='nuevo': un lead al que solo se le puso el
  // responsable ya paso por aqui y no debe reaparecer en la cola.
  // has_transcript va porque sin conversacion no hay nada que leer ni que decidir; es la
  // misma condicion que cuenta sin_etiquetas en /api/stats, para que cuadren.
  const params = new URLSearchParams({
    sin_etiquetas: 'true', has_transcript: 'true', limit: 200, offset: 0,
  });
  aplicarRango(params);
  const data = await api('GET', '/leads?' + params);
  if (!data) return;

  $('tag-count').textContent = `${data.total} pendiente${data.total !== 1 ? 's' : ''}`;

  if (!data.items.length) {
    // Distinguir «no queda nada» de «no queda nada EN ESTE RANGO». Sin esto, filtrar una
    // semana tranquila diria «todo etiquetado» con 186 leads esperando, y nadie volveria.
    listEl.innerHTML = hayRango()
      ? `<div class="empty">${ico('check-circle')}<p>No quedan leads pendientes de etiquetar entre ${esc(textoRango())}.</p>
           <button type="button" class="btn btn-secondary btn-sm" onclick="limpiarRango()">Ver todo el pendiente</button></div>`
      : `<div class="empty">${ico('check-circle')}<p>No quedan leads pendientes de etiquetar.</p></div>`;
    $('tag-panel').innerHTML = `<div class="empty">${ico('check-circle')}<p>Todo etiquetado.</p></div>`;
    $('tag-grid').classList.remove('show-panel');
    tagSelected = null;
    return;
  }
  if (!data.items.some(l => l.lead_id === tagSelected)) tagSelected = data.items[0].lead_id;

  listEl.innerHTML = data.items.map(l => {
    const nombre = nombreLead(l);
    const sub = [l.company_name, fmtDate(l.captured_at), l.message_count ? `${l.message_count} mensajes` : '']
      .filter(Boolean).map(esc).join(' · ');
    return `<button type="button" class="tag-item${l.lead_id === tagSelected ? ' active' : ''}" data-id="${esc(l.lead_id)}" onclick="selectTag('${esc(l.lead_id)}')">
      ${avatar(nombre, '', l.lead_id)}
      <span class="tag-item-body"><span class="t-name">${esc(nombre || '+' + l.lead_id)}</span><span class="t-sub">${sub}</span></span>
    </button>`;
  }).join('');

  loadTagPanel(tagSelected);
}

function selectTag(leadId) {
  tagSelected = leadId;
  $('tag-grid').classList.add('show-panel');   // en el celular: pasa de la lista al panel
  loadEtiquetado();
}

function volverListaTag() {
  $('tag-grid').classList.remove('show-panel');
  loadEtiquetado();
}

/* Pasa al lead que sigue en la cola. Si el actual acaba de recibir etiqueta, al recargar
   ya no estara; si se salto sin etiquetar, sigue ahi y se pasa al de al lado. */
function siguientePendiente() {
  const ids = [...document.querySelectorAll('.tag-item')].map(b => b.dataset.id);
  const i = ids.indexOf(tagSelected);
  const sig = ids[i + 1] || ids[0];
  if (!sig || sig === tagSelected) { loadEtiquetado(); return; }
  selectTag(sig);
}

function moverSeleccionTag(delta) {
  const ids = [...document.querySelectorAll('.tag-item')].map(b => b.dataset.id);
  if (!ids.length) return;
  const i = Math.max(0, ids.indexOf(tagSelected));
  const j = Math.min(ids.length - 1, Math.max(0, i + delta));
  if (ids[j] !== tagSelected) selectTag(ids[j]);
}

async function loadTagPanel(leadId) {
  const panel = $('tag-panel');
  panel.innerHTML = '<div class="loading"><span class="spinner"></span>Cargando…</div>';
  const l = await api('GET', `/leads/${leadId}`);
  if (!l || l.detail) return;
  const nombre = nombreLead(l);
  panel.innerHTML = `
    <button type="button" class="btn btn-ghost btn-sm tag-back" onclick="volverListaTag()">${ico('arrow-left', 'ico-sm')}Volver a la lista</button>
    <div class="panel-title">
      <span class="row">${avatar(nombre, 'avatar-sm', l.lead_id)}${esc(nombre || '+' + l.lead_id)}</span>
      <a href="#detail/${esc(l.lead_id)}" class="hint" title="Abrir el detalle"><code>+${esc(l.lead_id)}</code></a>
    </div>
    ${renderTranscript(l.transcript)}
    <div class="panel-title" style="margin-top:18px">¿Cuál fue el resultado?</div>
    ${tagPicker(l.lead_id, tagsOf(l), 'tag')}
    <div class="tag-panel-foot">
      <span class="hint">Marca el estado real. Si no se puede decidir todavía, pasa al siguiente.</span>
      <button type="button" class="btn btn-secondary btn-sm" id="tag-next" onclick="siguientePendiente()">Siguiente pendiente${ico('arrow-right', 'ico-sm')}</button>
    </div>`;
}

/* ══════════ Brief (documento estructurado) ══════════ */
function kvGrid(rows) {
  return `<div class="bd-kv">${rows.map(r => `<b>${esc(r.label)}</b><span>${esc(r.value)}</span>`).join('')}</div>`;
}

function renderBriefDoc(b) {
  const gen = new Date(b.generado);
  const fecha = gen.toLocaleDateString('es-PE', { day: '2-digit', month: 'long', year: 'numeric' });
  const prob = (b.conversion_prob != null) ? Math.round(b.conversion_prob * 100) : null;
  const leadRows = [
    { label: 'Teléfono', value: b.lead.telefono },
    b.lead.nombre  && { label: 'Nombre',  value: b.lead.nombre },
    b.lead.empresa && { label: 'Empresa', value: b.lead.empresa },
    b.lead.email   && { label: 'Email',   value: b.lead.email },
    b.lead.ruc     && { label: 'RUC',     value: b.lead.ruc },
  ].filter(Boolean);

  return `<div class="brief-doc" id="brief-doc">
    <div class="bd-head">
      <div class="bd-brand"><b>MAU</b><div>Lead Scoring · Contatech</div></div>
      <div class="bd-meta">Documento generado<br>${esc(fecha)}</div>
    </div>
    <div class="bd-title">${esc(b.titulo)}</div>
    <div class="bd-sub">
      ${b.qualified ? '<span class="badge badge-green">Calificado</span>' : '<span class="badge badge-gray">No calificado</span>'}
      ${prob != null ? `<span class="badge badge-blue">P(conversión): ${prob}%</span>` : ''}
    </div>
    <div class="bd-section"><h4>Datos de contacto</h4>${kvGrid(leadRows)}</div>
    ${b.perfil.length ? `<div class="bd-section"><h4>Perfil del negocio</h4>${kvGrid(b.perfil)}</div>` : ''}
    ${b.contexto.length ? `<div class="bd-section"><h4>Contexto comercial</h4>${kvGrid(b.contexto)}</div>` : ''}
    ${b.modulos.length ? `<div class="bd-section"><h4>Módulos recomendados</h4>${
      b.modulos.map(m => `<div class="bd-mod"><b>${esc(m.nombre)}</b> — ${esc(m.desc)}</div>`).join('')
    }</div>` : ''}
    ${b.seguimiento ? `<div class="bd-section"><h4>Siguiente paso</h4>
      <div class="bd-next">${esc(b.seguimiento.texto)}<br>
        <span style="font-size:12.5px;color:var(--ink-2)">
          Para el ${esc(fmtDia(b.seguimiento.fecha))}
          ${b.seguimiento.vencido ? ' · <b style="color:var(--crit)">VENCIDO</b>' : ''}
          ${b.seguimiento.autor ? ' · definido por ' + esc(cap(b.seguimiento.autor)) : ''}
        </span>
      </div>
    </div>` : ''}
    ${b.ultima_nota ? `<div class="bd-section"><h4>Última nota</h4>
      <div class="bd-nota">${esc(b.ultima_nota.texto)}<span class="firma">— ${esc(cap(b.ultima_nota.autor))}, ${esc(fmtFechaHora(b.ultima_nota.fecha))}</span></div>
    </div>` : ''}
    <div class="bd-section"><h4>Acción sugerida</h4>
      <div class="bd-next">${esc(b.siguiente_paso.accion)}<br><a href="${esc(b.siguiente_paso.link)}" target="_blank" rel="noopener">${esc(b.siguiente_paso.link)}</a></div>
    </div>
    <div class="bd-foot">Generado automáticamente por MAU Lead Scoring — uso interno del equipo comercial.</div>
  </div>`;
}

async function showBrief(leadId) {
  $('brief-pdf-btn').hidden = true;
  $('brief-body').innerHTML = '<div class="modal-loading"><span class="spinner"></span>Generando brief…</div>';
  $('brief-overlay').classList.add('open');
  const data = await api('POST', `/leads/${leadId}/brief`);
  if (!data) return;
  if (!data.brief) {
    $('brief-body').innerHTML = `<div class="empty">${ico('alert')}<p>No se pudo generar el brief.</p></div>`;
    return;
  }
  $('brief-body').innerHTML = renderBriefDoc(data.brief);
  $('brief-pdf-btn').hidden = false;
}

function closeBrief() { cerrarModal('brief-overlay'); }

/* ══════════ Eventos globales ══════════ */

// Clic fuera cierra popovers y menus; scroll cierra el popover de etiquetas, que al estar
// en position:fixed no sigue a su fila.
document.addEventListener('click', e => {
  cerrarTagPops();
  cerrarMenus();
  if (!e.target.closest('#periodo-pop') && !e.target.closest('.periodo-chip')) cerrarPeriodo();
});
$('periodo-pop').addEventListener('click', e => e.stopPropagation());
window.addEventListener('scroll', cerrarTagPops, true);

// Clic en el fondo cierra el modal; el login no, que sin sesion no hay a donde volver.
document.querySelectorAll('.overlay:not(#auth-overlay)').forEach(o => {
  o.addEventListener('click', e => { if (e.target === o) o.classList.remove('open'); });
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { onEscape(); return; }
  // Ctrl+K (o / fuera de un campo) enfoca el buscador desde cualquier vista.
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); enfocarBuscador(); return; }
  if (e.key === '/' && !enCampo(e.target) && token()) { e.preventDefault(); enfocarBuscador(); return; }
  // En la cola de etiquetado, las flechas recorren la lista.
  if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && vistaActual() === 'etiquetado' && !enCampo(e.target)) {
    e.preventDefault();
    moverSeleccionTag(e.key === 'ArrowDown' ? 1 : -1);
  }
});

$('q-inp').addEventListener('blur', () => setTimeout(ocultarResultados, 150));

// La barra superior gana borde cuando el contenido pasa por debajo.
function onScrollTop() {
  const desplazado = $('main').scrollTop > 4 || window.scrollY > 4;
  $('topbar').classList.toggle('scrolled', desplazado);
}
$('main').addEventListener('scroll', onScrollTop, { passive: true });
window.addEventListener('scroll', onScrollTop, { passive: true });
// Sombra en la columna Lead cuando la tabla se desplaza en horizontal.
$('table-wrap').addEventListener('scroll', function () {
  this.classList.toggle('scrolled-x', this.scrollLeft > 0);
}, { passive: true });
window.addEventListener('resize', aplicarSidebar);

// Init
renderTagFiltros();   // antes de cualquier route(): loadLeads() lee esos desplegables
montarOjosClave();
aplicarSidebar();
renderUsuario();
if (token()) {
  $('auth-overlay').classList.remove('open');
  route();
} else {
  $('user-inp').focus();
}
