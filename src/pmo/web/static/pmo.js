/* ==========================================================================
   PMO La Traccia — comportamento dell'applicazione

   Tre cose sole, tutte con le regole del design system:
   - i messaggi transitori (.tr-toast): chiusura automatica solo per le conferme,
     con la durata minima di 4 secondi piu' uno ogni cinque parole oltre le dieci;
     il conto si ferma sotto il puntatore e col fuoco dentro; un errore o
     un'allerta non si chiude da solo;
   - l'attivazione della listbox del select (js/tr-select.js);
   - copia negli appunti e conferma prima delle azioni distruttive.
   ========================================================================== */
(function () {
  'use strict';

  /* ---- Messaggi transitori ------------------------------------------------ */

  function durata(toast) {
    var testo = (toast.textContent || '').trim();
    var parole = testo ? testo.split(/\s+/).length : 0;
    return 4000 + Math.floor(Math.max(0, parole - 10) / 5) * 1000;
  }

  function chiudi(toast) {
    if (toast && toast.parentNode) { toast.parentNode.removeChild(toast); }
  }

  function monta(toast) {
    var chiusura = toast.querySelector('[data-toast-close]');
    if (chiusura) { chiusura.addEventListener('click', function () { chiudi(toast); }); }

    // Errori e allerte: solo a mano. Idem se il messaggio porta un comando.
    var manuale = toast.getAttribute('role') === 'alert' || toast.querySelector('.tr-btn:not([data-toast-close])');
    if (manuale) { return; }

    var restante = durata(toast);
    var inizio = null;
    var timer = null;
    function avvia() {
      inizio = Date.now();
      timer = setTimeout(function () { chiudi(toast); }, restante);
    }
    function ferma() {
      if (timer === null) { return; }
      clearTimeout(timer);
      timer = null;
      restante = Math.max(1000, restante - (Date.now() - inizio));
    }
    toast.addEventListener('mouseenter', ferma);
    toast.addEventListener('mouseleave', function () { if (timer === null) { avvia(); } });
    toast.addEventListener('focusin', ferma);
    toast.addEventListener('focusout', function () { if (timer === null) { avvia(); } });
    avvia();
  }

  var TITOLI = { success: 'Fatto', info: 'Informazione', warning: 'Attenzione', danger: 'Errore' };

  function nuovoToast(variante, messaggio, titolo) {
    var zona = document.getElementById('pmo-toasts');
    if (!zona) { return; }
    var t = document.createElement('div');
    t.className = 'tr-toast tr-toast--' + variante;
    if (variante === 'danger' || variante === 'warning') { t.setAttribute('role', 'alert'); }
    t.innerHTML =
      '<span class="tr-toast__icon" aria-hidden="true"></span>' +
      '<div class="tr-toast__content"><p class="tr-toast__title"></p><p class="tr-toast__body"></p></div>' +
      '<button class="tr-btn tr-btn--icon tr-btn--sm" type="button" aria-label="Chiudi il messaggio" data-toast-close>' +
      '<span class="tr-icon tr-icon--close" aria-hidden="true"></span></button>';
    t.querySelector('.tr-toast__title').textContent = titolo || TITOLI[variante] || '';
    t.querySelector('.tr-toast__body').textContent = messaggio;
    zona.appendChild(t);
    monta(t);
  }

  /* ---- Viste di una pagina di dettaglio ------------------------------------
     Pattern ARIA delle tabs: frecce, Home e Fine spostano la scelta; la vista scelta
     resta nell'indirizzo (#messaggi) e, dopo un'azione, si torna su quella. */

  function viste() {
    var cont = document.querySelector('[data-viste]');
    if (!cont) { return; }
    var tabs = Array.prototype.slice.call(document.querySelectorAll('[data-viste-tabs] [role="tab"]'));

    function attiva(tab, conFocus, scorri) {
      tabs.forEach(function (t) {
        var scelta = t === tab;
        t.setAttribute('aria-selected', scelta ? 'true' : 'false');
        t.tabIndex = scelta ? 0 : -1;
        var pannello = document.getElementById(t.getAttribute('aria-controls'));
        if (pannello) { pannello.classList.toggle('is-attiva', scelta); }
      });
      if (conFocus) { tab.focus(); }
      var nome = tab.id.replace('tab-', '');
      try { history.replaceState(null, '', '#' + nome); } catch (e) { /* resta la vista corrente */ }
      // le tabs restano agganciate in cima: se si era scesi dentro una vista lunga, la nuova parte dall'inizio
      var barra = document.querySelector('[data-viste-tabs]');
      var sotto = barra ? barra.getBoundingClientRect().bottom : 0;
      if (scorri && cont.getBoundingClientRect().top < sotto) {
        window.scrollBy(0, cont.getBoundingClientRect().top - sotto - 16);
      }
    }

    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { attiva(t, false, true); });
      t.addEventListener('keydown', function (e) {
        var prossima = null;
        if (e.key === 'ArrowRight') { prossima = tabs[(i + 1) % tabs.length]; }
        else if (e.key === 'ArrowLeft') { prossima = tabs[(i - 1 + tabs.length) % tabs.length]; }
        else if (e.key === 'Home') { prossima = tabs[0]; }
        else if (e.key === 'End') { prossima = tabs[tabs.length - 1]; }
        if (prossima) { e.preventDefault(); attiva(prossima, true, true); }
      });
    });

    // Si parte sempre dalla prima vista (la Scheda); solo un indirizzo con #vista, come il ritorno dopo
    // un'azione o un collegamento diretto, apre quella.
    var iniziale = location.hash.slice(1);
    for (var k = 0; k < tabs.length; k++) {
      if (tabs[k].id === 'tab-' + iniziale) { attiva(tabs[k], false); break; }
    }

    cont.addEventListener('submit', function (e) {
      var ritorno = e.target.querySelector('input[name="ritorno"]');
      var attiva_ = cont.querySelector('.pmo-vista.is-attiva');
      if (ritorno && attiva_ && ritorno.value.indexOf('#') < 0) {
        ritorno.value += '#' + attiva_.id.replace('vista-', '');
      }
    });
  }

  function copiaClassica(testo) {
    var campo = document.createElement('textarea');
    campo.value = testo;
    campo.setAttribute('readonly', '');
    campo.setAttribute('aria-hidden', 'true');
    campo.style.position = 'fixed';
    campo.style.opacity = '0';
    document.body.appendChild(campo);
    campo.select();
    var riuscita = false;
    try { riuscita = document.execCommand('copy'); } catch (e) { riuscita = false; }
    document.body.removeChild(campo);
    return riuscita;
  }

  /* ---- Avvio -------------------------------------------------------------- */

  function avvio() {
    Array.prototype.forEach.call(document.querySelectorAll('.tr-toast'), monta);

    if (window.TrSelect) { window.TrSelect.enhanceAll(); }
    viste();

    Array.prototype.forEach.call(document.querySelectorAll('[data-copia]'), function (el) {
      el.addEventListener('click', function () {
        var testo = el.getAttribute('data-copia');
        var messaggio = el.getAttribute('data-copia-messaggio') || 'Oggetto della mail copiato negli appunti.';
        var fatto = function () { nuovoToast('success', messaggio, 'Copiato'); };
        var errore = function () { nuovoToast('danger', 'Copia non riuscita: seleziona il testo e copialo a mano.'); };
        // L'API moderna puo' mancare o essere negata (indirizzo non sicuro, permessi): si ripiega sul
        // metodo classico, con un campo di testo temporaneo, e solo se fallisce anche quello si avvisa.
        var classico = function () { if (copiaClassica(testo)) { fatto(); } else { errore(); } };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(testo).then(fatto, classico);
        } else { classico(); }
      });
    });

    // Azioni lunghe: il pulsante dichiara l'attesa (aria-busy) e mostra la Sequenza del
    // sistema, tre chevron che si accendono in fila. Cambia il testo, non la scatola.
    Array.prototype.forEach.call(document.querySelectorAll('form[data-attesa]'), function (f) {
      f.addEventListener('submit', function (e) {
        if (e.defaultPrevented) { return; }
        var b = f.querySelector('button[type="submit"]');
        if (!b) { return; }
        var chevron = '<svg class="tr-spinner__mark" width="12" height="15" viewBox="0 0 594 717" aria-hidden="true"><use href="#tr-mark"/></svg>';
        b.setAttribute('aria-busy', 'true');
        b.innerHTML = '<span class="tr-spinner tr-spinner--inherit"><span class="tr-spinner__seq">' + chevron + chevron + chevron + '</span></span><span></span>';
        b.lastChild.textContent = f.getAttribute('data-attesa');
      });
    });

    Array.prototype.forEach.call(document.querySelectorAll('form[data-conferma]'), function (f) {
      f.addEventListener('submit', function (e) {
        if (!window.confirm(f.getAttribute('data-conferma'))) { e.preventDefault(); }
      });
    });

    // Fase in chips: "Cambia fase" si accende solo quando la chip scelta non e' la fase attuale.
    Array.prototype.forEach.call(document.querySelectorAll('form[data-fase]'), function (f) {
      var attuale = f.getAttribute('data-fase');
      var b = f.querySelector('button[type="submit"]');
      if (!b) { return; }
      var aggiorna = function () {
        var scelta = f.querySelector('input[name="stato"]:checked');
        b.disabled = !scelta || scelta.value === attuale;
      };
      f.addEventListener('change', aggiorna);
      aggiorna();
    });

    // Tendine dentro le tabelle che scorrono (.pmo-scroll): il contenitore taglierebbe il pannello aperto, sia
    // verso il basso sia verso l'alto, e la tabella sopra lo coprirebbe. Aperto, il pannello si aggancia alla
    // finestra (position: fixed) alla posizione del campo, e si allarga quanto serve per leggere le voci.
    var allinea = function (field) {
      var panel = field.querySelector('.tr-select__panel');
      var trigger = field.querySelector('.tr-select__trigger');
      if (!panel || !trigger) { return; }
      if (field.getAttribute('data-open') !== 'true') {
        ['position', 'left', 'right', 'top', 'bottom', 'width', 'minWidth', 'maxWidth', 'zIndex'].forEach(function (p) { panel.style[p] = ''; });
        return;
      }
      var r = trigger.getBoundingClientRect();
      panel.style.position = 'fixed';
      panel.style.left = r.left + 'px';
      panel.style.right = 'auto';
      panel.style.minWidth = r.width + 'px';
      panel.style.width = 'max-content';
      panel.style.maxWidth = Math.max(r.width, Math.min(480, window.innerWidth - r.left - 16)) + 'px';
      panel.style.zIndex = '60';
      if (field.getAttribute('data-drop') === 'up') {
        panel.style.top = 'auto';
        panel.style.bottom = (window.innerHeight - r.top + 6) + 'px';
      } else {
        panel.style.bottom = 'auto';
        panel.style.top = (r.bottom + 6) + 'px';
      }
    };
    var tendine = document.querySelectorAll('.pmo-scroll .tr-select');
    if (tendine.length && window.MutationObserver) {
      var osservatore = new MutationObserver(function (cambi) {
        cambi.forEach(function (c) { allinea(c.target); });
      });
      Array.prototype.forEach.call(tendine, function (field) {
        osservatore.observe(field, { attributes: true, attributeFilter: ['data-open', 'data-drop'] });
      });
      var riallinea = function () {
        Array.prototype.forEach.call(document.querySelectorAll('.pmo-scroll .tr-select[data-open="true"]'), allinea);
      };
      window.addEventListener('scroll', riallinea, true);
      window.addEventListener('resize', riallinea);
    }

    // Righe di offerte e ordini, colonna "Dal listino". Scelto un articolo: codice e descrizione diventano quelli
    // del listino e si bloccano; prezzo e IVA si copiano se vuoti o ancora uguali all'articolo di prima (un prezzo
    // trattato resta). "Riga libera" sblocca codice e descrizione. Il nome intero dell'articolo scelto, che nel
    // menu stretto si legge a meta', compare come suggerimento passandoci sopra.
    Array.prototype.forEach.call(document.querySelectorAll('select[data-listino]'), function (s) {
      var modulo = s.getAttribute('form');
      var precedente = s.options[s.selectedIndex] || null;
      var bloccati = { modulo: 'codice', descrizione: 'descrizione' };
      var liberi = { prezzo_unitario: 'prezzo', aliquota_iva: 'iva' };
      var campo = function (nome) { return document.getElementById(modulo + '-' + nome); };
      var suggerimento = function () {
        var scelta = s.options[s.selectedIndex];
        var testo = scelta && scelta.value ? scelta.text : 'Riga libera: codice, descrizione e prezzo si scrivono a mano';
        var trigger = document.getElementById(s.id + '-trigger');
        [s, trigger].forEach(function (el) { if (el) { el.title = testo; } });
      };
      var blocca = function (attivo) {
        Object.keys(bloccati).forEach(function (nome) {
          var input = campo(nome);
          if (!input) { return; }
          input.readOnly = attivo;
          if (attivo) { input.title = 'Dal listino: si cambia in Impostazioni → Listino oppure scegliendo Riga libera'; }
          else { input.removeAttribute('title'); }
        });
      };
      suggerimento();
      s.addEventListener('change', function () {
        var scelta = s.options[s.selectedIndex];
        var articolo = scelta && scelta.value;
        if (articolo) {
          Object.keys(bloccati).forEach(function (nome) {
            var input = campo(nome);
            if (input) { input.value = scelta.getAttribute('data-' + bloccati[nome]) || ''; }
          });
          Object.keys(liberi).forEach(function (nome) {
            var input = campo(nome);
            if (!input) { return; }
            var prima = precedente && precedente.value ? (precedente.getAttribute('data-' + liberi[nome]) || '') : '';
            if (input.value === '' || input.value === prima) { input.value = scelta.getAttribute('data-' + liberi[nome]) || ''; }
          });
        }
        blocca(!!articolo);
        suggerimento();
        precedente = scelta;
      });
    });
  }

  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', avvio); }
  else { avvio(); }

  window.pmoToast = nuovoToast;
})();
