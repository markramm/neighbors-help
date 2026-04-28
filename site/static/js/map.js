/* Neighbors Help — map controller (per-state two-tier).

   Tier 1 (default on load):
     - Fetch /data/index.json (small: state list + per-petal counts)
     - Render state-level daisy markers at US zoom
     - Sidebar lists states sorted by org count

   Tier 2 (on state pick or zip search):
     - Fetch /data/states/{XX}.json (lazy, cached after first load)
     - Render zip-level daisies (zoom < THRESHOLD) or petal pins (>= THRESHOLD)
     - Sidebar lists in-state resources by zip
     - "Back to US" returns to tier 1

   Data shapes:
     index.json     { states: [{code,name,lat,lng,counts,total,zip_count}], petals, total_orgs, total_zips }
     states/XX.json { code, name, center, counts, total, zip_count, zips: { "12345": { center, orgs:[...] } } }
     coverage.json  { "12345": { present:[...], missing:[...] } }
     zip-prefix.json{ "480": "MI", ... }   first 3 digits → state
*/
(function () {
  'use strict';

  // Tiered zoom model:
  //   zoom < CITY_TO_ZIP_ZOOM      -> city-aggregated daisies
  //   zoom < ZIP_TO_PIN_ZOOM       -> per-zip daisies
  //   zoom >= ZIP_TO_PIN_ZOOM      -> individual petal pins
  const CITY_TO_ZIP_ZOOM = 9;     // switch from city to zip view
  const ZIP_TO_PIN_ZOOM  = 12;    // switch from zip daisies to per-org pins
  // At very low state-view zooms, only show the busiest cities to avoid
  // a wall of overlapping markers. Below this count → suppressed.
  const CITY_MIN_COUNT_AT_LOW_ZOOM = 3;
  const LOW_ZOOM_CUTOFF = 7;       // zoom <= this is "low"
  const US_VIEW = { center: [39.5, -98.5], zoom: 4 };

  const PETALS = window.PETALS || [];
  const PETAL_BY_ID = Object.fromEntries(PETALS.map(p => [p.id, p]));
  const PETAL_ORDER = PETALS.map(p => p.id);

  const state = {
    scope: 'us',                  // 'us' | 'state'
    stateCode: null,              // currently loaded state, when scope='state'
    scopedZip: null,              // when set (only on /z/{zip}/ pages), all
                                  // sidebar/marker rendering filters to this
                                  // zip only. Distinct from `selectedZip`,
                                  // which just controls the detail panel.
    index: null,                  // contents of /data/index.json
    coverage: {},                 // contents of /data/coverage.json
    zipPrefix: {},                // first-3 → state lookup
    stateCache: {},               // { XX: parsed JSON }
    activePetals: new Set(PETAL_ORDER),
    query: '',
    selectedZip: null,
    zoom: US_VIEW.zoom,
  };

  let map;
  let markerLayer;

  // ============= bootstrap =============
  Promise.all([
    fetch('/data/index.json').then(r => r.json()),
    fetch('/data/coverage.json').then(r => r.json()).catch(() => ({})),
    fetch('/data/zip-prefix.json').then(r => r.json()).catch(() => ({})),
  ]).then(([index, coverage, zipPrefix]) => {
    state.index = index;
    state.coverage = coverage;
    state.zipPrefix = zipPrefix;
    initMap();
    bindUI();
    // Honor server-side deep links (set by /s/{XX}/ and /z/{12345}/ pages),
    // then fall back to URL query params, then to the default US view.
    const initial = window.NH_INITIAL || parseUrlDeepLink();
    if (initial && initial.zip) {
      // On /z/{zip}/ pages we want the in-zip experience by default:
      // sidebar shows only the zip's resources, map lands zoomed-in,
      // detail panel opens. The state JSON still loads (so the user can
      // un-scope by clicking elsewhere), but it's never rendered as a
      // wide layer first.
      state.scopedZip = initial.zip;
      loadState(initial.state);
    } else if (initial && initial.state) {
      loadState(initial.state);
    } else {
      renderUS();
    }
  }).catch(err => {
    console.error('failed to load index data:', err);
    document.getElementById('results').innerHTML =
      '<div class="empty">Could not load data. Try refreshing.</div>';
  });

  function parseUrlDeepLink() {
    const u = new URL(window.location.href);
    const stateParam = u.searchParams.get('state');
    const zipParam = u.searchParams.get('zip');
    if (zipParam && /^\d{5}$/.test(zipParam)) {
      const st = state.zipPrefix && state.zipPrefix[zipParam.slice(0, 3)];
      if (st) return { state: st, zip: zipParam };
    }
    if (stateParam && /^[A-Za-z]{2}$/.test(stateParam)) {
      return { state: stateParam.toUpperCase() };
    }
    return null;
  }


  // ============= map =============
  function initMap() {
    map = L.map('map', {
      center: US_VIEW.center,
      zoom: US_VIEW.zoom,
      zoomControl: true,
      scrollWheelZoom: true,
      worldCopyJump: false,
      minZoom: 3,
    });
    L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
      attribution: '© OpenStreetMap © CARTO',
      subdomains: 'abcd',
      maxZoom: 18,
    }).addTo(map);

    map.on('zoomend moveend', () => {
      state.zoom = map.getZoom();
      updateBadge();
      if (state.scope === 'state') redrawState();
    });
    state.zoom = map.getZoom();
    updateBadge();
  }

  function updateBadge() {
    const el = document.getElementById('map-mode');
    if (!el) return;
    if (state.scope === 'us') {
      el.innerHTML = '<b>States</b> · pick one to zoom in';
    } else if (state.scopedZip) {
      el.innerHTML = `<b>${escapeText(state.scopedZip)}</b>`;
    } else if (state.zoom >= ZIP_TO_PIN_ZOOM) {
      el.innerHTML = '<b>Resources</b> · zoom out for groups';
    } else if (state.zoom >= CITY_TO_ZIP_ZOOM) {
      el.innerHTML = '<b>Zip codes</b> · zoom in for resources';
    } else {
      el.innerHTML = '<b>Cities</b> · zoom in for zip codes';
    }
  }

  function clearLayer() {
    if (markerLayer) {
      map.removeLayer(markerLayer);
      markerLayer = null;
    }
  }

  // ============= US tier =============
  function renderUS() {
    state.scope = 'us';
    state.stateCode = null;
    state.selectedZip = null;
    if (window.location.pathname === '/') {
      try { history.replaceState(null, '', '/'); } catch (_) {}
    }
    map.setView(US_VIEW.center, US_VIEW.zoom, { animate: true });
    clearLayer();
    markerLayer = L.layerGroup().addTo(map);

    const summaries = (state.index?.states || []).filter(s =>
      [...state.activePetals].some(p => (s.counts || {})[p])
    );

    summaries.forEach(s => addStateDaisy(s));
    renderUSSidebar(summaries);
    renderCounts(summaries.flatMap(s => Object.entries(s.counts || {}).map(([k, v]) => ({k, v}))));
    closeDaisyPanel();
    updateBadge();
    updateBackButton();
  }

  function addStateDaisy(s) {
    const present = [...state.activePetals].filter(p => (s.counts || {})[p]);
    if (!present.length) return;
    const total = [...state.activePetals].reduce((sum, p) => sum + ((s.counts || {})[p] || 0), 0);
    if (!total) return;
    const size = 56 + Math.min(40, Math.log2(total + 1) * 8);
    const html = `<div class="daisy-marker"><div class="daisy-float"><div class="bloom">${
      petalSvg(present, total, size, { compact: true })
    }</div></div></div>`;
    const icon = L.divIcon({
      html, className: 'daisy-div-icon',
      iconSize: [size, size], iconAnchor: [size / 2, size / 2],
    });
    const marker = L.marker([s.lat, s.lng], { icon, riseOnHover: true });
    marker.bindTooltip(`${s.name} · ${total} resources`, { direction: 'top', offset: [0, -size/2] });
    marker.on('click', () => loadState(s.code));
    marker.addTo(markerLayer);
  }

  // ============= per-state tier =============
  function loadState(code, then) {
    if (!code) return;
    code = code.toUpperCase();
    if (!state.stateCache[code]) {
      const resultsEl = document.getElementById('results');
      if (resultsEl) resultsEl.innerHTML = `<div class="empty">Loading ${code}…</div>`;
      fetch(`/data/states/${code}.json`).then(r => r.json()).then(data => {
        state.stateCache[code] = data;
        enterState(code);
        if (then) then();
      }).catch(err => {
        console.error('failed to load state', code, err);
        if (resultsEl) resultsEl.innerHTML = `<div class="empty">Couldn't load ${code}.</div>`;
      });
    } else {
      enterState(code);
      if (then) then();
    }
  }

  function enterState(code) {
    state.scope = 'state';
    state.stateCode = code;
    state.selectedZip = null;
    // Update URL bar without full reload, but only on homepage. (On /s/ and
    // /z/ pages, navigating in-place would lose Hugo-rendered OG tags.)
    if (window.location.pathname === '/') {
      try {
        const u = new URL(window.location.href);
        u.searchParams.set('state', code);
        u.searchParams.delete('zip');
        history.replaceState(null, '', u.toString());
      } catch (_) { /* noop */ }
    }
    const data = state.stateCache[code];
    if (!data) return;

    // If we're scoped to a specific zip (deep link from /z/{zip}/), jump
    // straight in: fit the map to the actual org coords in that zip, open
    // its detail panel, render only its pins. Skip the state-wide layer
    // entirely.
    if (state.scopedZip) {
      const grp = data.zips?.[state.scopedZip];
      if (grp?.orgs && grp.orgs.length > 0) {
        // Fit to the bounding box of the actual orgs in this zip — center
        // alone misses pins that are at the edge of a wide zip area.
        const pts = grp.orgs
          .filter(o => typeof o.lat === 'number' && typeof o.lng === 'number')
          .map(o => [o.lat, o.lng]);
        if (pts.length === 1) {
          map.setView(pts[0], 15, { animate: true });
        } else {
          const bounds = L.latLngBounds(pts);
          map.fitBounds(bounds, { padding: [60, 60], maxZoom: 16 });
        }
        state.selectedZip = state.scopedZip;
      } else if (grp?.center) {
        map.setView([grp.center.lat, grp.center.lng], 13, { animate: true });
        state.selectedZip = state.scopedZip;
      } else if (data.center) {
        // Zip not found in state's data — fall back to state center.
        map.setView([data.center.lat, data.center.lng], 9, { animate: true });
        state.scopedZip = null;
      }
    } else {
      // Default state landing: anchor on the authoritative state centroid
      // at a fixed state-level zoom. (Earlier I tried fitBounds across zip
      // centers, but a state with one dense zip + few sparse zips would
      // collapse to a tight box around the dense one.)
      const c = data.center;
      if (c) {
        // Pick zoom by physical state size — small states get higher zoom.
        // Use the spread of zip centers as a rough proxy for state size.
        const pts = Object.values(data.zips || {})
          .map(g => g.center)
          .filter(p => p && typeof p.lat === 'number' && typeof p.lng === 'number');
        let z = 7;
        if (pts.length >= 2) {
          const lats = pts.map(p => p.lat), lngs = pts.map(p => p.lng);
          const span = Math.max(
            Math.max(...lats) - Math.min(...lats),
            (Math.max(...lngs) - Math.min(...lngs)) * Math.cos(c.lat * Math.PI / 180),
          );
          // span ≈ degrees; pick zoom heuristically
          z = span > 8 ? 5 : span > 4 ? 6 : span > 2 ? 7 : span > 1 ? 8 : 9;
        }
        map.setView([c.lat, c.lng], z, { animate: true });
      }
    }
    redrawState();
    updateBackButton();
  }

  function redrawState() {
    if (state.scope !== 'state') return;
    const data = state.stateCache[state.stateCode];
    if (!data) return;
    clearLayer();
    markerLayer = L.layerGroup().addTo(map);

    const orgs = filteredStateOrgs(data);

    // When scoped to a single zip (deep-linked from /z/{zip}/), always
    // render individual pins regardless of zoom — the user is here to see
    // those specific resources, not zoom-based aggregation.
    if (state.scopedZip) {
      orgs.forEach(o => addPetalPin(o));
    } else if (state.zoom >= ZIP_TO_PIN_ZOOM) {
      // Tier 3: individual petal pins
      orgs.forEach(o => addPetalPin(o));
    } else if (state.zoom >= CITY_TO_ZIP_ZOOM) {
      // Tier 2: per-zip daisies
      const grouped = {};
      orgs.forEach(o => {
        (grouped[o.zip] ||= { orgs: [], center: o.zipCenter }).orgs.push(o);
      });
      for (const zip in grouped) {
        const g = grouped[zip];
        if (!g.center) continue;
        addZipDaisy(zip, g.center, g.orgs);
      }
    } else {
      // Tier 1: per-city daisies, filtered to busiest cities at very low zoom
      addCityDaisies(data, orgs);
    }

    renderStateSidebar(data, orgs);
    renderCounts(stateCountsFor(data));
    renderDaisyPanel();
    updateBadge();
  }

  function addCityDaisies(data, orgs) {
    // Group filtered orgs by city, then look up authoritative city center
    // from data.cities (computed at build time).
    const orgsByCity = {};
    orgs.forEach(o => {
      const cname = (o.city || '').trim();
      if (!cname) return;
      const key = cname.toLowerCase();
      (orgsByCity[key] ||= []).push(o);
    });
    const cities = data.cities || {};
    const lowZoom = state.zoom <= LOW_ZOOM_CUTOFF;
    for (const key in orgsByCity) {
      const orgsInCity = orgsByCity[key];
      if (lowZoom && orgsInCity.length < CITY_MIN_COUNT_AT_LOW_ZOOM) continue;
      const meta = cities[key];
      // Use authoritative center from build-time aggregation if we can.
      // Fall back to averaging filtered orgs (which may be a subset).
      let center;
      if (meta && meta.center) {
        center = meta.center;
      } else {
        const lat = orgsInCity.reduce((s, o) => s + o.lat, 0) / orgsInCity.length;
        const lng = orgsInCity.reduce((s, o) => s + o.lng, 0) / orgsInCity.length;
        center = { name: orgsInCity[0].city || '', lat, lng };
      }
      addCityDaisy(key, center, orgsInCity);
    }
  }

  function addCityDaisy(cityKey, center, orgs) {
    const petals = [...new Set(orgs.map(o => o.type))];
    // Size scales with org count, but with a smaller range than zip daisies
    // because there are more of them and they're at lower zoom.
    const size = 50 + Math.min(40, Math.log2(orgs.length + 1) * 9);
    const html = `<div class="daisy-marker"><div class="daisy-float"><div class="bloom">${
      petalSvg(petals, orgs.length, size, { compact: true })
    }</div></div></div>`;
    const icon = L.divIcon({
      html, className: 'daisy-div-icon',
      iconSize: [size, size], iconAnchor: [size / 2, size / 2],
    });
    const marker = L.marker([center.lat, center.lng], { icon, riseOnHover: true });
    marker.bindTooltip(`${center.name} · ${orgs.length} resources`,
      { direction: 'top', offset: [0, -size / 2] });
    marker.on('click', () => {
      // Zoom in to the city to expose its zip daisies
      map.setView([center.lat, center.lng], CITY_TO_ZIP_ZOOM, { animate: true });
    });
    marker.addTo(markerLayer);
  }

  function filteredStateOrgs(data) {
    const out = [];
    const q = state.query.trim().toLowerCase();
    const zips = state.scopedZip
      ? (data.zips?.[state.scopedZip] ? [state.scopedZip] : [])
      : Object.keys(data.zips || {});
    for (const zip of zips) {
      const grp = data.zips[zip];
      if (!grp) continue;
      for (const org of grp.orgs) {
        if (!state.activePetals.has(org.type)) continue;
        if (q) {
          const hay = (
            (org.name || '') + ' ' + zip + ' ' + (grp.center?.name || '') + ' ' +
            (org.address || '') + ' ' + (org.city || '')
          ).toLowerCase();
          if (!hay.includes(q)) continue;
        }
        out.push({ ...org, zip, zipCenter: grp.center });
      }
    }
    return out;
  }

  function stateCountsFor(data) {
    return Object.entries(data.counts || {}).map(([k, v]) => ({ k, v }));
  }

  function addZipDaisy(zip, center, orgs) {
    const petals = [...new Set(orgs.map(o => o.type))];
    const size = 64 + Math.min(18, orgs.length * 2);
    const html = `<div class="daisy-marker"><div class="daisy-float"><div class="bloom">${
      petalSvg(petals, orgs.length, size)
    }</div></div></div>`;
    const icon = L.divIcon({
      html, className: 'daisy-div-icon',
      iconSize: [size, size], iconAnchor: [size / 2, size / 2],
    });
    const marker = L.marker([center.lat, center.lng], { icon, riseOnHover: true });
    marker.on('click', () => {
      state.selectedZip = zip;
      map.setView([center.lat, center.lng], Math.max(13, state.zoom + 1), { animate: true });
      renderDaisyPanel();
    });
    marker.addTo(markerLayer);
  }

  function addPetalPin(org) {
    const size = 36;
    const html = `<div class="petal-marker"><div class="drop">${petalPinSvg(org.type, size)}</div></div>`;
    const icon = L.divIcon({
      html, className: 'petal-div-icon',
      iconSize: [size, size * 1.2], iconAnchor: [size / 2, size * 1.15],
    });
    const marker = L.marker([org.lat, org.lng], { icon });
    marker.bindPopup(orgPopupHtml(org), { maxWidth: 320, closeButton: false, offset: [0, -size * 0.8] });
    marker.addTo(markerLayer);
  }

  // ============= SVG markers =============
  function petalSvg(petals, count, size, opts) {
    const half = size / 2;
    const r = size * 0.30;
    const petalR = size * 0.16;
    const centerR = size * 0.17;
    const present = new Set(petals);
    const slots = PETAL_ORDER.map((p, i) => {
      const angleDeg = i * (360 / PETAL_ORDER.length) - 90;
      const angleRad = angleDeg * Math.PI / 180;
      const cx = half + r * Math.cos(angleRad);
      const cy = half + r * Math.sin(angleRad);
      const color = present.has(p) ? `var(${PETAL_BY_ID[p].cssVar})` : 'oklch(0.88 0.01 60)';
      return `<circle cx="${cx}" cy="${cy}" r="${petalR}" fill="${color}" stroke="oklch(0.25 0.03 60 / 0.35)" stroke-width="1"/>`;
    }).join('');
    // For US-tier compact daisies, count font shrinks since values can be 4-5 digits
    const fontSize = opts && opts.compact ? size * 0.18 : size * 0.22;
    return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" style="overflow:visible">
      ${slots}
      <circle cx="${half}" cy="${half}" r="${centerR}" fill="var(--yellow)" stroke="oklch(0.25 0.03 60 / 0.5)" stroke-width="1.2"/>
      <text x="${half}" y="${half + size * 0.07}" text-anchor="middle"
        font-family="Lora, serif" font-size="${fontSize}" font-weight="600"
        fill="oklch(0.22 0.02 60)">${count}</text>
    </svg>`;
  }

  function petalPinSvg(petalId, size) {
    const color = `var(${PETAL_BY_ID[petalId]?.cssVar || ('--' + petalId)})`;
    const w = size, h = size * 1.2;
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
      <defs><filter id="shd-${petalId}">
        <feDropShadow dx="0" dy="2" stdDeviation="1.5" flood-color="oklch(0.2 0.04 60)" flood-opacity="0.28"/>
      </filter></defs>
      <path d="M ${size/2} ${size*1.15} L ${size*0.3} ${size*0.72} A ${size*0.4} ${size*0.4} 0 1 1 ${size*0.7} ${size*0.72} Z"
        fill="${color}" stroke="oklch(0.25 0.03 60 / 0.45)" stroke-width="1.2" filter="url(#shd-${petalId})"/>
      <circle cx="${size/2}" cy="${size*0.44}" r="${size*0.18}" fill="var(--paper)" stroke="oklch(0.25 0.03 60 / 0.25)" stroke-width="0.8"/>
    </svg>`;
  }

  function orgPopupHtml(org) {
    const p = PETAL_BY_ID[org.type] || { label: org.type, cssVar: '--ink' };
    const web = org.website
      ? `<dt>Website</dt><dd><a href="${escapeAttr(org.website)}" rel="noopener" target="_blank">${escapeText(org.website.replace(/^https?:\/\//, '').replace(/\/$/, ''))}</a></dd>`
      : '';
    const phone = org.phone ? `<dt>Phone</dt><dd>${escapeText(org.phone)}</dd>` : '';
    const hours = org.hours ? `<dt>Hours</dt><dd>${escapeText(org.hours)}</dd>` : '';
    return `<div class="org-popup">
      <div class="op-head"><span class="op-dot" style="background: var(${p.cssVar})"></span>${escapeText(p.label)}</div>
      <h3>${escapeText(org.name)}</h3>
      <dl>${hours}${phone}<dt>Where</dt><dd>${escapeText((org.address ? org.address + ', ' : '') + (org.city || '') + ', ' + (org.state || '').toUpperCase() + ' ' + org.zip)}</dd>${web}</dl>
      <div class="op-actions"><a href="${escapeAttr(org.url)}">Full details →</a></div>
    </div>`;
  }

  // ============= sidebars =============
  function renderUSSidebar(summaries) {
    const root = document.getElementById('results');
    const countEl = document.getElementById('result-count');
    if (countEl) countEl.textContent = `(${summaries.length} states)`;

    const total = summaries.reduce((s, x) => s + x.total, 0);
    const sorted = [...summaries].sort((a, b) => b.total - a.total);
    const cards = sorted.map(s => {
      const dots = PETALS.filter(p => (s.counts || {})[p.id] && state.activePetals.has(p.id))
        .map(p => `<span class="rc-dot" style="background:var(${p.cssVar})" title="${p.label}"></span>`)
        .join('');
      return `<a class="result-card state-card" href="#${s.code}" data-state="${s.code}">
        <div class="rc-head">
          <span style="display:inline-flex;gap:3px;align-items:center;margin-right:6px">${dots}</span>
          <span style="flex:1">${escapeText(s.name)}</span>
          <span class="rc-zip">${s.total}</span>
        </div>
        <div class="rc-blurb">${s.zip_count} zips · pick to zoom</div>
      </a>`;
    }).join('');

    root.innerHTML = `
      <div class="zip-group">
        <div class="zip-header">${total.toLocaleString()} resources · ${summaries.length} states</div>
        <div class="results">${cards}</div>
      </div>`;

    root.querySelectorAll('.state-card').forEach(card => {
      card.addEventListener('click', e => {
        e.preventDefault();
        loadState(card.dataset.state);
      });
    });
  }

  function renderStateSidebar(data, orgs) {
    const root = document.getElementById('results');
    const countEl = document.getElementById('result-count');
    if (countEl) countEl.textContent = `(${orgs.length})`;

    if (!orgs.length) {
      root.innerHTML = '<div class="empty">No resources match. Try clearing filters or widening search.</div>';
      return;
    }
    const byZip = {};
    orgs.forEach(o => { (byZip[o.zip] ||= { orgs: [], center: o.zipCenter }).orgs.push(o); });
    const zips = Object.keys(byZip).sort((a, b) => byZip[b].orgs.length - byZip[a].orgs.length);

    // Cap rendered zips to keep DOM responsive on very dense states.
    // The map shows everything; the sidebar shows the top 30 unless searching.
    const MAX_ZIPS = state.query ? zips.length : 30;
    const visible = zips.slice(0, MAX_ZIPS);
    const truncated = zips.length - visible.length;

    const html = visible.map(zip => {
      const g = byZip[zip];
      const cards = g.orgs.slice(0, 20).map(o => {
        const p = PETAL_BY_ID[o.type] || { cssVar: '--ink' };
        return `<a class="result-card" href="${escapeAttr(o.url)}" data-lat="${o.lat}" data-lng="${o.lng}">
          <div class="rc-head">
            <span class="rc-dot" style="background: var(${p.cssVar})"></span>
            <span style="flex:1">${escapeText(o.name)}</span>
          </div>
          <div class="rc-blurb">${escapeText((o.address ? o.address + ' · ' : '') + (o.hours || ''))}</div>
        </a>`;
      }).join('');
      const more = g.orgs.length > 20
        ? `<div style="font-size:12px; color:var(--ink-mute); padding:4px 6px;">+ ${g.orgs.length - 20} more in ${zip}</div>`
        : '';
      return `<div class="zip-group">
        <div class="zip-header">${escapeText((g.center?.name || 'Unknown') + ' · ' + zip + ' · ' + g.orgs.length)}</div>
        <div class="results">${cards}${more}</div>
      </div>`;
    }).join('');

    const footer = truncated > 0
      ? `<div class="empty">+ ${truncated} more zips with resources. Type a zip or town to filter.</div>`
      : '';

    root.innerHTML = html + footer;

    root.querySelectorAll('.result-card').forEach(card => {
      card.addEventListener('mouseenter', () => {
        const lat = parseFloat(card.dataset.lat), lng = parseFloat(card.dataset.lng);
        if (!isNaN(lat) && !isNaN(lng)) {
          map.setView([lat, lng], Math.max(state.zoom, 14), { animate: true });
        }
      });
    });
  }

  function renderCounts(petalCounts) {
    // petalCounts: array of { k: petalId, v: count }
    const counts = {};
    petalCounts.forEach(({ k, v }) => { counts[k] = (counts[k] || 0) + v; });
    PETALS.forEach(p => {
      const el = document.querySelector(`[data-count="${p.id}"]`);
      if (el) el.textContent = (counts[p.id] || 0).toLocaleString();
    });
  }

  // ============= daisy detail panel =============
  function renderDaisyPanel() {
    const mount = document.getElementById('daisy-panel-mount');
    if (!state.selectedZip || state.scope !== 'state') {
      mount.innerHTML = '';
      return;
    }
    const data = state.stateCache[state.stateCode];
    const grp = data?.zips?.[state.selectedZip];
    if (!grp) { mount.innerHTML = ''; return; }

    const orgs = grp.orgs.filter(o => state.activePetals.has(o.type));
    const byPetal = {};
    orgs.forEach(o => { (byPetal[o.type] ||= []).push(o); });
    const cov = state.coverage[state.selectedZip] || { present: [], missing: [] };

    const sections = PETALS.filter(p => byPetal[p.id]).map(p => {
      const list = byPetal[p.id].map(o => `
        <a class="panel-org" href="${escapeAttr(o.url)}">
          <div class="pn">${escapeText(o.name)}</div>
          <div class="pb">${escapeText((o.address || '') + (o.hours ? ' · ' + o.hours : ''))}</div>
        </a>`).join('');
      return `<div class="petal-group">
        <h4><span class="pg-dot" style="background: var(${p.cssVar})"></span>${escapeText(p.label)} · ${byPetal[p.id].length}</h4>
        ${list}
      </div>`;
    }).join('');

    const missingChips = (cov.missing || []).map(m => {
      const p = PETAL_BY_ID[m]; if (!p) return '';
      return `<span style="padding:4px 10px; font-size:12px; border-radius:999px; background:var(--paper-2); border:1px dashed var(--line); color:var(--ink-soft); display:inline-flex; align-items:center; gap:6px;">
        <span style="width:8px;height:8px;border-radius:50%;background:var(${p.cssVar});opacity:0.5"></span>
        No ${escapeText(p.label.toLowerCase())} listed
      </span>`;
    }).join('');

    const gapsBlock = (cov.missing && cov.missing.length) ? `
      <div class="petal-group">
        <h4 style="color:var(--ink-mute)">Gaps here</h4>
        <div style="display:flex; flex-wrap:wrap; gap:6px; padding:0 2px">${missingChips}</div>
        <div style="font-size:12px; color:var(--ink-mute); margin-top:8px; line-height:1.45">
          Know one? <a href="${escapeAttr(window.NH_REPO + '/issues/new?template=add-resource.md&title=Add+resource+for+' + state.selectedZip)}" target="_blank" rel="noopener">Add it →</a>
        </div>
      </div>` : '';

    mount.innerHTML = `<div class="daisy-panel">
      <header>
        <div>
          <p class="eyebrow">${escapeText(state.selectedZip)}</p>
          <h2>${escapeText(grp.center?.name || 'This zip')}</h2>
        </div>
        <button class="close-x" id="close-daisy" aria-label="Close">×</button>
      </header>
      <div class="panel-body">
        <div class="stat-row">
          <div class="stat"><div class="sv">${orgs.length}</div><div class="sl">Resources</div></div>
          <div class="stat"><div class="sv">${(cov.present || []).length}/${PETALS.length}</div><div class="sl">Petals</div></div>
          <div class="stat"><div class="sv">${(cov.missing || []).length}</div><div class="sl">Gaps</div></div>
        </div>
        ${sections}
        ${gapsBlock}
      </div>
    </div>`;
    document.getElementById('close-daisy').addEventListener('click', closeDaisyPanel);
  }

  function closeDaisyPanel() {
    state.selectedZip = null;
    const mount = document.getElementById('daisy-panel-mount');
    if (mount) mount.innerHTML = '';
  }

  // ============= back-to-US button =============
  function updateBackButton() {
    const el = document.getElementById('back-to-us');
    if (!el) return;
    if (state.scope === 'state') {
      const code = state.stateCode || '';
      const stateMeta = (state.index?.states || []).find(s => s.code === code);
      el.style.display = '';
      el.querySelector('.bb-label').textContent = stateMeta?.name || code;
    } else {
      el.style.display = 'none';
    }
  }

  // ============= UI bindings =============
  function bindUI() {
    // petal filters
    document.querySelectorAll('.petal-filter').forEach(cb => {
      cb.addEventListener('change', () => {
        const id = cb.dataset.petal;
        if (cb.checked) state.activePetals.add(id); else state.activePetals.delete(id);
        if (state.scope === 'us') renderUS(); else redrawState();
      });
    });

    // search box
    const search = document.getElementById('search');
    if (search) {
      search.addEventListener('input', () => onSearch(search.value));
      search.addEventListener('keydown', e => {
        if (e.key === 'Enter') onSearch(search.value, true);
      });
    }

    // state selector dropdown
    const sel = document.getElementById('state-selector');
    if (sel) {
      // Populate with sorted state names
      const opts = ['<option value="">Pick a state…</option>']
        .concat((state.index?.states || [])
          .sort((a, b) => a.name.localeCompare(b.name))
          .map(s => `<option value="${s.code}">${escapeText(s.name)} (${s.total})</option>`));
      sel.innerHTML = opts.join('');
      // Pre-select the current state if we're on /s/{XX}/ or /z/{12345}/
      if (window.NH_INITIAL && window.NH_INITIAL.state) {
        sel.value = window.NH_INITIAL.state;
      }
      sel.addEventListener('change', () => {
        if (!sel.value) return;
        // On /s/ and /z/ pages, navigating to a new state should update the
        // URL bar — drive a full nav. On the homepage, just switch in place.
        if (window.location.pathname === '/') {
          loadState(sel.value);
        } else {
          window.location.href = `/s/${sel.value}/`;
        }
      });
    }

    // back-to-US button. Only intercept the click on the homepage (where
    // the JS can switch state in place). On /s/{XX}/ and /z/{12345}/ pages
    // the link has a real href to /; let the browser navigate normally so
    // the URL bar updates.
    const back = document.getElementById('back-to-us');
    if (back && window.location.pathname === '/') {
      back.addEventListener('click', e => { e.preventDefault(); renderUS(); });
    }
  }

  function onSearch(raw, jumpStrong = false) {
    state.query = raw || '';
    const q = state.query.trim();
    // Zip → state lookup. If valid 5-digit zip, resolve via prefix.
    if (/^\d{5}$/.test(q)) {
      const targetState = state.zipPrefix[q.slice(0, 3)];
      if (targetState) {
        if (state.scope !== 'state' || state.stateCode !== targetState) {
          loadState(targetState);
          // After state loads, the zip will get matched via in-state search
          // because state.query is already set. enterState() calls redrawState
          // which calls filteredStateOrgs, which uses state.query.
          return;
        }
        // Already in the right state — try to focus the matching zip
        const data = state.stateCache[targetState];
        const grp = data?.zips?.[q];
        if (grp?.center) {
          map.setView([grp.center.lat, grp.center.lng], 12, { animate: true });
        }
      }
    }
    if (state.scope === 'us') {
      // Filter the state list by name
      const summaries = (state.index?.states || []).filter(s =>
        !q || s.name.toLowerCase().includes(q.toLowerCase()) || s.code.toLowerCase() === q.toLowerCase()
      );
      renderUSSidebar(summaries);
    } else {
      redrawState();
    }
  }

  // ============= helpers =============
  function escapeText(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
  }
  function escapeAttr(s) { return escapeText(s); }
})();
