(function () {
  'use strict';
  var sid = location.pathname.replace(/\/+$/, '').split('/').pop();
  var API = '../api/j/' + encodeURIComponent(sid);
  var STORE_KEY = 'jielong:' + sid;
  var ERRORS = {
    closed: '已经截单了', full: '接龙已满 50 人', name: '名字要 1–20 个字', note: '备注最多 40 字',
    items: '请选择餐品', qty: '数量不对', unknown_code: '有餐品已下架，请刷新', cap: '超过每人上限',
    rate_limited: '操作太频繁，稍后再试', demo_limit: '演示环境人太多了，稍后再试', forbidden: '没有权限', not_found: '这条已不存在',
    bad_request: '请求有误'
  };

  var data = null;
  var skew = 0;           // server time - client time (ms)
  var cart = {};          // code -> qty
  var tabIdx = 0;
  var editing = false;
  var busy = false;
  var writeVersion = 0;   // bumped after every successful write; polls started earlier are stale
  var errMsg = '';
  var mine = loadMine();
  var nameVal = '';
  var noteVal = '';

  var root = document.getElementById('app');

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function yuan(fen) {
    var s = (fen / 100).toFixed(2).replace(/\.?0+$/, '');
    return '¥' + s;
  }
  // expectDeliveryTime is a clock time (20:45, 2026-10-14 20:45:00) or plain text (立即送出)
  function deliveryText(t) {
    var s = String(t).trim();
    var m = /^(?:\d{4}-\d{1,2}-\d{1,2}[ T])?(\d{1,2}:\d{2})(?::\d{2})?$/.exec(s);
    return m ? '预计 ' + m[1] + ' 送达' : '配送：' + s;
  }
  function loadMine() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY)) || null; } catch (e) { return null; }
  }
  function saveMine(v) {
    mine = v;
    try {
      if (v) localStorage.setItem(STORE_KEY, JSON.stringify(v)); else localStorage.removeItem(STORE_KEY);
    } catch (e) { /* private mode */ }
  }
  function nowMs() { return Date.now() + skew; }
  function remainingMs() { return Date.parse(data.cutoff_at) - nowMs(); }
  function isOpen() { return data.state === 'open' && remainingMs() > 0; }

  function cartTotal() {
    var t = 0;
    Object.keys(cart).forEach(function (c) {
      var it = data.menu.items[c];
      if (it) t += it.price_fen * cart[c];
    });
    return t;
  }
  function cartItems() {
    return Object.keys(cart).filter(function (c) { return cart[c] > 0; })
      .map(function (c) { return { code: c, qty: cart[c] }; });
  }
  function myEntry() {
    if (!mine) return null;
    for (var i = 0; i < data.entries.length; i++) if (data.entries[i].id === mine.id) return data.entries[i];
    return null;
  }

  function request(method, path, body, secret) {
    var opts = { method: method, headers: {} };
    if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    if (secret) opts.headers['X-Entry-Secret'] = secret;
    return fetch(API + path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, body: j }; });
    });
  }

  // ---- sections (rebuilt independently so typing is not interrupted) ----
  var elHead = el('div', 'hero'), elList = el('div'), elOrder = el('div'), elClosed = el('div');
  var elSel, elTabs, elItems, elBtn, elErr, elName, elNote, elMineBox, elSumAmt, elSumTip;

  function renderHead() {
    clear(elHead);
    elHead.appendChild(el('h1', null, '🍔 ' + data.title));
    elHead.appendChild(el('div', 'sub', data.store_name + ' → ' + data.address_label));
    var tags = el('div', 'tags');
    var cd = el('span', 'tag hot');
    cd.id = 'countdown';
    tags.appendChild(cd);
    if (data.cap_fen) tags.appendChild(el('span', 'tag', '每人 ≤ ' + yuan(data.cap_fen)));
    elHead.appendChild(tags);
    tickCountdown();
  }
  function tickCountdown() {
    var cd = document.getElementById('countdown');
    if (!cd) return;
    if (data.state !== 'open') { cd.textContent = '已截单'; return; }
    var ms = remainingMs();
    if (ms <= 0) { cd.textContent = '已截单'; return; }
    var m = Math.floor(ms / 60000), s = Math.floor(ms / 1000) % 60;
    cd.textContent = m >= 1 ? '还剩 ' + m + ' 分钟截单' : '还剩 ' + s + ' 秒截单';
  }

  function renderList() {
    clear(elList);
    var box = el('div', 'box');
    var top = el('div', 'row head');
    top.appendChild(el('b', null, '接龙（' + data.count + ' 人）'));
    top.appendChild(el('span', 'muted', '合计 ' + yuan(data.total_fen)));
    box.appendChild(top);
    if (!data.entries.length) box.appendChild(el('p', 'muted empty', '还没有人，来第一个吧'));
    data.entries.forEach(function (e) {
      var isMine = mine && mine.id === e.id;
      var row = el('div', 'entry' + (isMine ? ' mine' : ''));
      row.appendChild(el('i', 'seq', String(e.seq)));
      var body = el('div', 'body');
      var who = el('div', 'who', e.name);
      if (isMine) who.appendChild(el('span', 'me', '我'));
      body.appendChild(who);
      var what = el('div', 'what');
      what.appendChild(document.createTextNode(e.items.map(function (r) {
        return r.name + (r.qty > 1 ? ' ×' + r.qty : '');
      }).join(' + ')));
      if (e.note) what.appendChild(el('div', 'note', '备注：' + e.note));
      body.appendChild(what);
      row.appendChild(body);
      row.appendChild(el('span', 'amt', yuan(e.subtotal_fen)));
      box.appendChild(row);
    });
    elList.appendChild(box);
  }

  function renderClosed() {
    clear(elClosed);
    if (isOpen()) return;
    var box = el('div', 'box state');
    var st = data.state;
    var msg;
    if (st === 'ordered') {
      msg = '已下单' + (data.order_status ? ' · ' + data.order_status : '') +
        (data.expect_delivery_time ? ' · ' + deliveryText(data.expect_delivery_time) : '');
    } else if (st === 'ordering') msg = '发起人正在下单…';
    else if (st === 'uncertain') msg = '下单结果确认中，请稍候';
    else if (st === 'cancelled') msg = '这个团已取消';
    else msg = '已截单，等发起人下单';
    box.appendChild(el('b', null, msg));
    elClosed.appendChild(box);
  }

  function renderOrder() {
    clear(elOrder);
    if (!isOpen()) return;
    var mineEntry = myEntry();
    if (mineEntry && !editing) {
      var box = el('div', 'box mine-box');
      box.appendChild(el('div', null, '你已加入：' + mineEntry.items.map(function (r) {
        return r.name + (r.qty > 1 ? ' ×' + r.qty : '');
      }).join(' + ') + '（' + yuan(mineEntry.subtotal_fen) + '）'));
      var acts = el('div', 'acts');
      var b1 = el('button', 'btn2', '修改'); b1.type = 'button';
      b1.onclick = function () { startEdit(mineEntry); };
      var b2 = el('button', 'btn2', '撤回我的'); b2.type = 'button';
      b2.onclick = withdraw;
      acts.appendChild(b1); acts.appendChild(b2);
      box.appendChild(acts);
      elMineBox = el('div', 'err'); box.appendChild(elMineBox);
      if (errMsg) elMineBox.textContent = errMsg;
      elOrder.appendChild(box);
      return;
    }
    buildComposer();
  }

  function buildComposer() {
    var box = el('div', 'box composer');
    elName = el('input'); elName.type = 'text'; elName.placeholder = '你的名字'; elName.maxLength = 20;
    elName.value = nameVal;
    elName.oninput = function () { nameVal = elName.value; };
    box.appendChild(elName);
    var menu = el('div', 'menu');
    elTabs = el('div', 'tabs'); menu.appendChild(elTabs);
    elItems = el('div', 'items'); menu.appendChild(elItems);
    box.appendChild(menu);
    elNote = el('input'); elNote.type = 'text'; elNote.placeholder = '备注（例：不要酱；大家都能看到，不会发给麦当劳）'; elNote.maxLength = 40;
    elNote.value = noteVal;
    elNote.oninput = function () { noteVal = elNote.value; };
    box.appendChild(elNote);
    elSel = el('div', 'sel'); box.appendChild(elSel);
    if (editing) {
      var c = el('button', 'btn2', '取消修改'); c.type = 'button';
      c.onclick = function () { editing = false; errMsg = ''; cart = {}; renderOrder(); };
      box.appendChild(c);
    }
    // floating cart bar: total on the left, submit on the right
    var bar = el('div', 'cartbar');
    elErr = el('div', 'err'); bar.appendChild(elErr);
    var pill = el('div', 'pill');
    var sum = el('div', 'sum');
    elSumAmt = el('b', null); elSumTip = el('span', null);
    sum.appendChild(elSumAmt); sum.appendChild(elSumTip);
    pill.appendChild(sum);
    elBtn = el('button', 'go'); elBtn.type = 'button'; elBtn.onclick = submit;
    pill.appendChild(elBtn);
    bar.appendChild(pill);
    box.appendChild(bar);
    elOrder.appendChild(box);
    renderTabs(); renderItems(); renderBtn();
  }

  function renderTabs() {
    clear(elTabs);
    data.menu.categories.forEach(function (c, i) {
      var t = el('span', 'tab' + (i === tabIdx ? ' on' : ''), c.name);
      var n = 0;
      c.codes.forEach(function (code) { n += cart[code] || 0; });
      if (n > 0) t.appendChild(el('i', 'badge', String(n)));
      t.onclick = function () { tabIdx = i; renderTabs(); renderItems(); elItems.scrollTop = 0; };
      elTabs.appendChild(t);
    });
  }
  function renderItems() {
    clear(elItems);
    var cat = data.menu.categories[tabIdx];
    if (!cat) return;
    cat.codes.forEach(function (code) {
      var it = data.menu.items[code];
      var row = el('div', 'item');
      if (typeof it.image === 'string' && it.image.indexOf('https://') === 0) {
        var img = document.createElement('img');
        img.className = 'thumb'; img.alt = ''; img.loading = 'lazy';
        img.referrerPolicy = 'no-referrer';
        img.addEventListener('error', function () { img.style.display = 'none'; });
        img.src = it.image;
        row.appendChild(img);
      }
      var info = el('div', 'info');
      info.appendChild(el('span', 'nm', it.name));
      var foot = el('div', 'foot');
      foot.appendChild(el('span', 'pr', yuan(it.price_fen)));
      var q = el('span', 'qty');
      var n = cart[code] || 0;
      if (n > 0) {
        var minus = el('button', 'round', '−'); minus.type = 'button';
        minus.onclick = function () { setQty(code, n - 1); };
        q.appendChild(minus);
        q.appendChild(el('b', null, String(n)));
      }
      var plus = el('button', 'round', '+'); plus.type = 'button';
      plus.disabled = n >= 10;
      plus.onclick = function () { setQty(code, n + 1); };
      q.appendChild(plus);
      foot.appendChild(q);
      info.appendChild(foot);
      row.appendChild(info);
      elItems.appendChild(row);
    });
  }
  function setQty(code, n) {
    if (n <= 0) delete cart[code]; else cart[code] = Math.min(n, 10);
    errMsg = '';
    renderTabs(); renderItems(); renderBtn();
  }
  function renderSel() {
    var parts = cartItems().map(function (r) {
      var it = data.menu.items[r.code];
      return (it ? it.name : r.code) + '×' + r.qty;
    });
    elSel.textContent = parts.length ? '已选：' + parts.join('、') : '';
  }
  function renderBtn() {
    renderSel();
    var total = cartTotal();
    var has = cartItems().length > 0;
    var over = data.cap_fen && total > data.cap_fen;
    var count = 0;
    cartItems().forEach(function (r) { count += r.qty; });
    elBtn.disabled = busy || !has || !!over;
    elBtn.textContent = busy ? '提交中…' : (editing ? '保存修改' : '加入接龙');
    elSumAmt.textContent = yuan(total);
    elSumTip.textContent = over ? '超过每人上限 ' + yuan(data.cap_fen)
      : (has ? '已选 ' + count + ' 件' : (data.cap_fen ? '每人上限 ' + yuan(data.cap_fen) : '还没选餐'));
    elSumTip.className = over ? 'over' : '';
    elErr.textContent = errMsg;
  }

  function startEdit(entry) {
    editing = true; errMsg = '';
    cart = {};
    entry.items.forEach(function (r) { cart[r.code] = r.qty; });
    nameVal = entry.name; noteVal = entry.note || '';
    renderOrder();
  }

  function showErr(res) {
    errMsg = ERRORS[res.body && res.body.error] || ('出错了（' + res.status + '）');
    if (elErr) elErr.textContent = errMsg;
    if (elMineBox) elMineBox.textContent = errMsg;
  }

  function submit() {
    if (busy) return;
    busy = true; errMsg = ''; renderBtn();
    var payload = { name: nameVal.trim(), items: cartItems(), note: noteVal.trim() };
    var p = editing && mine
      ? request('PUT', '/entries/' + mine.id, payload, mine.secret)
      : request('POST', '/entries', payload);
    p.then(function (res) {
      busy = false;
      if (res.status === 201) saveMine({ id: res.body.id, secret: res.body.secret });
      if (res.status === 201 || res.status === 200) {
        writeVersion++;
        editing = false; cart = {}; noteVal = ''; errMsg = ''; lastHasMine = null;
        return refresh();
      }
      showErr(res); if (elBtn) renderBtn();
      if (res.status === 409) return refresh();
    }).catch(function () {
      busy = false; errMsg = '网络不太好，再试一次'; if (elBtn) renderBtn();
    });
  }

  function withdraw() {
    if (!mine || busy) return;
    busy = true;
    request('DELETE', '/entries/' + mine.id, null, mine.secret).then(function (res) {
      busy = false;
      if (res.status === 200 || res.status === 404) {
        writeVersion++; saveMine(null); errMsg = ''; return refresh();
      }
      showErr(res);
    }).catch(function () { busy = false; errMsg = '网络不太好，再试一次'; if (elMineBox) elMineBox.textContent = errMsg; });
  }

  // ---- data loop ----
  function renderAll() {
    renderHead(); renderList(); renderClosed(); renderOrder();
  }
  var lastOpen = null, lastHasMine = null;
  function refresh() {
    var startedAt = writeVersion;
    return request('GET', '', null).then(function (res) {
      // A poll that began before our last write may predate it: never act on it
      // (it could make a just-created entry look absent), just poll again.
      if (startedAt !== writeVersion) { return refresh(); }
      if (res.status === 404) { clear(root); root.appendChild(el('p', 'muted', '这个接龙不存在或已过期')); return; }
      if (res.status !== 200) return;
      var firstLoad = data === null;
      data = res.body;
      skew = Date.parse(data.now) - Date.now();
      if (firstLoad) {
        clear(root);
        [elHead, elList, elClosed, elOrder].forEach(function (n) { root.appendChild(n); });
        renderAll();
        lastOpen = isOpen(); lastHasMine = !!myEntry();
        return;
      }
      renderHead(); renderList(); renderClosed();
      var open = isOpen();
      if (mine && !myEntry() && !editing) saveMine(null);   // gone server-side (fresh response only)
      var hasMine = !!myEntry();
      // rebuild the order area only on structural change, so typing is not lost
      if (open !== lastOpen || hasMine !== lastHasMine) renderOrder();
      lastOpen = open; lastHasMine = hasMine;
    }).catch(function () { /* keep polling */ });
  }

  // Demo banner: the server says which mode it runs in (no auth, no MCP).
  fetch('../api/meta', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (m) {
    if (!m || m.mode !== 'demo') return;
    var b = document.createElement('div');
    b.className = 'banner';
    b.textContent = '演示模式：数据是假的，不会真的下单';
    root.parentNode.insertBefore(b, root);
  }).catch(function () { /* banner is best-effort */ });

  refresh();
  setInterval(refresh, 5000);
  setInterval(function () {
    if (!data) return;
    tickCountdown();
    if (lastOpen && !isOpen()) { lastOpen = false; renderClosed(); renderOrder(); }
  }, 1000);
})();
