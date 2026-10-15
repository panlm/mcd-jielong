(function () {
  'use strict';
  var root = document.getElementById('app');
  var sid = new URLSearchParams(location.search).get('sid');
  var ERRORS = {
    address_id: '请选择地址', store_code: '请选择门店', be_code: '请选择门店', store_name: '请选择门店',
    cutoff_at: '截单时间要在现在之后', title: '名称要 1–20 个字', address_label: '地址简称要 1–30 个字',
    cap_fen: '每人上限要是正数', mcd: '麦当劳接口暂时不可用，稍后再试', bad_state: '当前状态不能这样操作',
    not_found: '这个团不存在或已过期', unauthorized: '没有权限：请用发起人链接打开（链接末尾带 #k=口令）',
    csrf: '请求被拒绝，请刷新页面再试',
    empty: '还没有人接龙，没法算价下单', busy: '正在下单中，请稍候，不要重复点',
    cancel_failed: '麦当劳没有同意取消，稍后点「刷新状态」看看', reason_code: '请先选择取消原因',
    order_id: '订单号不对', quote_fen: '请先算价', need_tableware: '餐具选项不对', remark: '备注不对',
    coupon: '这张券不能用，请重新选择', demo_limit: '演示环境人太多了（每小时最多开 5 个团），稍后再试',
    rate_limited: '操作太频繁，稍后再试',
    check_recent_first: '请先点「查最近订单」；查不到也请去麦当劳 App「我的订单」确认'
  };

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  // expectDeliveryTime is a clock time (20:45, 2026-10-14 20:45:00) or plain text (立即送出)
  function deliveryText(t) {
    var s = String(t).trim();
    var m = /^(?:\d{4}-\d{1,2}-\d{1,2}[ T])?(\d{1,2}:\d{2})(?::\d{2})?$/.exec(s);
    return m ? '预计 ' + m[1] + ' 送达' : '配送：' + s;
  }
  function yuan(fen) { return '¥' + (fen / 100).toFixed(2).replace(/\.?0+$/, ''); }
  // Every request carries this header: the server rejects /api/host/* without it (CSRF guard).
  function hostHeaders(json) {
    var h = { 'X-Requested-With': 'mcd-jielong' };
    if (json) h['Content-Type'] = 'application/json';
    return h;
  }
  function toRes(r) {
    return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, body: j }; });
  }
  function api(method, path, body) {
    var opts = { method: method, headers: hostHeaders(!!body), credentials: 'same-origin' };
    if (body) opts.body = JSON.stringify(body);
    return fetch('api/host/' + path, opts).then(toRes);
  }
  // The host link is .../host#k=<key>. A URL fragment never reaches any server or log. Take
  // the key out of the address bar (and history) first, then trade it for the HttpOnly cookie.
  function login() {
    var m = /^#k=(.+)$/.exec(location.hash || '');
    if (!m) return Promise.resolve(null);
    var key = m[1];
    try { key = decodeURIComponent(key); } catch (e) { /* keep it as typed */ }
    try { history.replaceState(null, '', location.pathname + location.search); } catch (e) { location.hash = ''; }
    return fetch('api/host/login', { method: 'POST', headers: hostHeaders(true), credentials: 'same-origin',
      body: JSON.stringify({ k: key }) }).then(toRes);
  }
  function banner(cls, text) {
    var b = el('div', cls, text);
    root.parentNode.insertBefore(b, root);
  }
  function errText(res) {
    var code = res.body && res.body.error;
    if (res.status === 401) return ERRORS.unauthorized;
    var text = ERRORS[code] || ('出错了（' + res.status + '）');
    if (code === 'mcd' && res.body.message) text += '：' + res.body.message;
    return text;
  }

  // Copy: Clipboard API, falling back to selecting a readonly input.
  function copyBox(text) {
    var wrap = el('div', 'box');
    var inp = el('input'); inp.type = 'text'; inp.readOnly = true; inp.value = text;
    var status = el('span', 'muted');
    var btn = el('button', 'btn2', '复制'); btn.type = 'button';
    function fallback() {
      inp.focus(); inp.select();
      try { inp.setSelectionRange(0, text.length); } catch (e) { /* ignore */ }
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      status.textContent = ok ? '已复制' : '已选中，请长按复制';
    }
    btn.onclick = function () {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { status.textContent = '已复制'; }, fallback);
      } else { fallback(); }
    };
    wrap.appendChild(inp);
    wrap.appendChild(btn);
    wrap.appendChild(status);
    return wrap;
  }

  // ---- Shanghai-time helpers (cutoff is always Asia/Shanghai) ----
  function shParts(ms) {
    var f = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai', hour12: false, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit'
    }).formatToParts(new Date(ms));
    var o = {};
    f.forEach(function (p) { o[p.type] = p.value; });
    if (o.hour === '24') o.hour = '00';
    return o;
  }
  function shHM(ms) { var p = shParts(ms); return p.hour + ':' + p.minute; }
  function cutoffIso(hm) {
    // hm "HH:MM" in Shanghai; if already past today, means tomorrow.
    var now = Date.now();
    var p = shParts(now);
    var iso = p.year + '-' + p.month + '-' + p.day + 'T' + hm + ':00+08:00';
    var t = Date.parse(iso);
    if (isNaN(t)) return null;
    if (t <= now) t += 24 * 3600 * 1000;
    var q = shParts(t);
    return q.year + '-' + q.month + '-' + q.day + 'T' + hm + ':00+08:00';
  }

  // ================= create view =================
  function createView() {
    clear(root);
    root.appendChild(el('h1', null, '新建加班补给'));
    var addresses = [], stores = [], pickedStore = null;

    var addrBox = el('div', 'box');
    addrBox.appendChild(el('div', 'muted', '送到'));
    var sel = el('select'); sel.style.width = '100%'; sel.style.padding = '8px';
    addrBox.appendChild(sel);
    root.appendChild(addrBox);

    var storeBox = el('div', 'box');
    storeBox.appendChild(el('div', 'muted', '门店（能送到的）'));
    var storeList = el('div');
    storeBox.appendChild(storeList);
    root.appendChild(storeBox);

    var form = el('div', 'box');
    function field(label, input) {
      var r = el('div'); r.appendChild(el('div', 'muted', label)); r.appendChild(input); return r;
    }
    var title = el('input'); title.type = 'text'; title.maxLength = 20; title.value = '我请客';
    var label = el('input'); label.type = 'text'; label.maxLength = 30;
    var cutoff = el('input'); cutoff.type = 'time'; cutoff.value = shHM(Date.now() + 40 * 60000);
    var cap = el('input'); cap.type = 'text'; cap.inputMode = 'decimal'; cap.value = '40'; cap.placeholder = '留空 = 不限';
    form.appendChild(field('显示名称', title));
    form.appendChild(field('地址简称（群友看到的）', label));
    form.appendChild(field('截单时间（北京时间）', cutoff));
    form.appendChild(field('每人上限（元，可留空）', cap));
    root.appendChild(form);

    var err = el('div', 'err');
    var go = el('button', 'btn', '生成接龙链接'); go.type = 'button';
    var result = el('div');
    root.appendChild(go); root.appendChild(err); root.appendChild(result);

    function setAddressLabel() {
      var a = addresses[sel.selectedIndex];
      label.value = a ? a.label : '';
    }
    function loadStores() {
      clear(storeList); pickedStore = null;
      var a = addresses[sel.selectedIndex];
      if (!a) return;
      storeList.appendChild(el('p', 'muted', '加载中…'));
      api('GET', 'stores?address_id=' + encodeURIComponent(a.address_id)).then(function (res) {
        clear(storeList);
        if (res.status !== 200) { storeList.appendChild(el('p', 'err', errText(res))); return; }
        stores = res.body;
        if (!stores.length) storeList.appendChild(el('p', 'muted', '这个地址没有能送到的门店'));
        stores.forEach(function (s, i) {
          var row = el('label'); row.style.display = 'block'; row.style.padding = '4px 0';
          var r = el('input'); r.type = 'radio'; r.name = 'store';
          r.onchange = function () { pickedStore = s; };
          row.appendChild(r);
          row.appendChild(document.createTextNode(' ' + s.name + ' '));
          row.appendChild(el('span', 'muted', '营业 ' + s.open + '–' + s.close));
          if (s.today) row.appendChild(el('div', 'muted', '今天：' + s.today));
          storeList.appendChild(row);
          if (i === 0) { r.checked = true; pickedStore = s; }
        });
      }).catch(function () { clear(storeList); storeList.appendChild(el('p', 'err', '网络不太好，刷新重试')); });
    }
    sel.onchange = function () { setAddressLabel(); loadStores(); };

    api('GET', 'addresses').then(function (res) {
      if (res.status !== 200) { addrBox.appendChild(el('p', 'err', errText(res))); return; }
      addresses = res.body;
      addresses.forEach(function (a) {
        var o = el('option', null, a.label); o.value = a.address_id; sel.appendChild(o);
      });
      setAddressLabel(); loadStores();
    }).catch(function () { addrBox.appendChild(el('p', 'err', '网络不太好，刷新重试')); });

    var busy = false;
    go.onclick = function () {
      if (busy) return;
      err.textContent = '';
      var a = addresses[sel.selectedIndex];
      if (!a) { err.textContent = ERRORS.address_id; return; }
      if (!pickedStore) { err.textContent = ERRORS.store_code; return; }
      var iso = cutoffIso(cutoff.value);
      if (!iso) { err.textContent = ERRORS.cutoff_at; return; }
      var capFen = null, c = cap.value.trim();
      if (c !== '') {
        var n = Number(c);
        if (!isFinite(n) || n <= 0) { err.textContent = ERRORS.cap_fen; return; }
        capFen = Math.round(n * 100);
      }
      busy = true; go.disabled = true;
      api('POST', 'sessions', {
        address_id: a.address_id, address_label: label.value.trim(),
        store_code: pickedStore.store_code, be_code: pickedStore.be_code, store_name: pickedStore.name,
        title: title.value.trim(), cutoff_at: iso, cap_fen: capFen
      }).then(function (res) {
        busy = false; go.disabled = false;
        if (res.status !== 201) { err.textContent = errText(res); return; }
        clear(result);
        result.appendChild(el('p', null, '已生成，把这个链接贴到群里：'));
        result.appendChild(copyBox(res.body.join_url));
        var manage = el('a', null, '去管理页（截单 / 下单）');
        manage.href = 'host?sid=' + encodeURIComponent(res.body.sid);
        result.appendChild(manage);
      }).catch(function () { busy = false; go.disabled = false; err.textContent = '网络不太好，再试一次'; });
    };
  }

  // ================= manage view =================
  function manageView() {
    clear(root);
    var data = null, busy = false, msg = '', headBuilt = false;
    var elHead = el('div'), elAct = el('div'), elList = el('div'), elOrder = el('div');
    elOrder.id = 'order-section'; // quote / confirm / pay / uncertain / cancel
    [elHead, elAct, elList, elOrder].forEach(function (n) { root.appendChild(n); });

    function renderHead() {
      clear(elHead);
      elHead.appendChild(el('h1', null, '管理页 · ' + data.title));
      var line = el('div', 'muted', data.store_name + ' → ' + data.address_label + ' · 截单 ' +
        shHM(Date.parse(data.cutoff_at)));
      if (data.cap_fen) line.appendChild(document.createTextNode(' · 每人 ≤ ' + yuan(data.cap_fen)));
      elHead.appendChild(line);
      var st = { open: '接龙中', locked: '已截单', ordering: '下单中', uncertain: '结果确认中',
        ordered: '已下单', cancelled: '已取消' }[data.state] || data.state;
      elHead.appendChild(el('div', 'hot', st));
      var link = location.origin + location.pathname.replace(/host\/?$/, '') + 'j/' + encodeURIComponent(sid);
      elHead.appendChild(copyBox(link));
    }
    function renderAct() {
      clear(elAct);
      if (data.state === 'open') {
        var b = el('button', 'btn2', '截单'); b.type = 'button'; b.disabled = busy;
        b.onclick = function () { act('lock'); };
        elAct.appendChild(b);
      } else if (data.state === 'locked' && (data.raw_state === 'open' || data.raw_state === 'locked')) {
        var r = el('button', 'btn2', '重新开放'); r.type = 'button'; r.disabled = busy;
        r.onclick = function () { act('reopen'); };
        elAct.appendChild(r);
      }
      if (msg) elAct.appendChild(el('div', 'err', msg));
    }
    function renderList() {
      clear(elList);
      var box = el('div', 'box');
      var top = el('div', 'row');
      top.appendChild(el('b', null, '接龙（' + data.count + ' 人）'));
      top.appendChild(el('span', 'muted', '合计 ' + yuan(data.total_fen)));
      box.appendChild(top);
      if (!data.entries.length) box.appendChild(el('p', 'muted', '还没有人接龙'));
      data.entries.forEach(function (e) {
        var row = el('div', 'entry');
        row.appendChild(el('span', 'who', e.seq + ' ' + e.name));
        var what = el('div', 'what');
        what.appendChild(document.createTextNode(e.items.map(function (r) {
          return r.name + (r.qty > 1 ? ' ×' + r.qty : '');
        }).join(' + ')));
        if (e.note) what.appendChild(el('div', 'note', '备注：' + e.note));
        row.appendChild(what);
        row.appendChild(el('span', null, yuan(e.subtotal_fen)));
        if (data.raw_state === 'open' || data.raw_state === 'locked') {
          var del = el('button', 'btn2 del', '删除'); del.type = 'button'; del.disabled = busy;
          del.onclick = function () { removeEntry(e); };
          row.appendChild(del);
        }
        box.appendChild(row);
      });
      elList.appendChild(box);
    }
    function removeEntry(e) {
      if (busy) return;
      if (!confirm('删除 ' + e.seq + ' 号「' + e.name + '」的接龙？\n删除后需要重新算价。')) return;
      busy = true; msg = ''; renderAct();
      api('DELETE', 'sessions/' + encodeURIComponent(sid) + '/entries/' + encodeURIComponent(e.id)).then(function (res) {
        busy = false;
        if (res.status !== 200) msg = errText(res);
        return refresh();
      }).catch(function () { busy = false; msg = '网络不太好，再试一次'; renderAct(); });
    }
    // ---------------- order section ----------------
    // Every MCP-backed call below is a POST fired by a button the host clicked; the 5 s poll
    // only reads our own server (GET sessions/<sid>).
    var REASONS = [['1', '改主意了'], ['2', '重复下单'], ['3', '点错了/点多了/点少了'],
      ['4', '地址或电话填错了'], ['5', '送达时间选错了'], ['-1', '其他']];
    var ord = { quote: null, quoteFor: '', need: true, remark: '', reason: '', busy: false,
      msg: '', note: '', recent: null, coupons: [], coupon: null };
    var orderKey = null;

    function entriesKey() { return data.count + ':' + data.total_fen; }
    function button(cls, text, onclick) {
      var b = el('button', cls, text); b.type = 'button'; b.disabled = ord.busy; b.onclick = onclick;
      return b;
    }
    function line(label, fen, strong) {
      var r = el('div', 'row');
      r.appendChild(el(strong ? 'b' : 'span', null, label));
      r.appendChild(el(strong ? 'b' : 'span', null, fen == null ? '—' : yuan(fen)));
      return r;
    }
    function orderRun(which, body, onRes) {
      if (ord.busy) return;
      ord.busy = true; ord.msg = ''; renderOrder();
      api('POST', 'sessions/' + encodeURIComponent(sid) + '/' + which, body).then(function (res) {
        ord.busy = false;
        onRes(res);
        renderOrder();
        return refresh();
      }).catch(function () {
        ord.busy = false;
        if (which === 'order') ord.quote = null; // outcome unknown: never reuse this confirmation
        ord.msg = which === 'order'
          ? '网络断了，不确定有没有下单成功。先等几秒看上面的状态，别急着再点。'
          : '网络不太好，再试一次';
        renderOrder();
        refresh();
      });
    }

    function doQuote() {
      orderRun('quote', { need_tableware: ord.need, coupon: ord.coupon }, function (res) {
        if (res.status === 200) {
          ord.quote = res.body; ord.quoteFor = entriesKey(); ord.note = '';
          ord.coupons = Array.isArray(res.body.coupons) ? res.body.coupons : [];
          ord.coupon = res.body.coupon || null; // what the server actually priced
        } else {
          ord.quote = null; // any non-200 (esp. 422 coupon): never keep a stale quote alive
          if (res.status === 422 && res.body && res.body.error === 'coupon') ord.coupon = null;
          ord.msg = errText(res);
        }
      });
    }
    function doOrder() {
      var q = ord.quote;
      if (!q) return;
      var amount = yuan(q.quote_fen); // the same number is shown, confirmed and sent
      if (!confirm('确认向麦当劳下单？\n应付 ' + amount + '\n下单后请尽快支付。')) return;
      orderRun('order', { quote_fen: q.quote_fen, remark: ord.remark, need_tableware: ord.need,
        coupon: q.coupon || null }, function (res) {
        // whatever happened, this confirmation is used up: ordering again needs a fresh 算价
        // and a fresh confirm() with the amount it shows
        ord.quote = null; ord.note = '';
        if (res.status === 200 || res.status === 202) return;
        var e = res.body && res.body.error;
        if (res.status === 409 && e === 'price_changed' && res.body.quote) {
          ord.note = '价格或接龙内容有变化：现在应付 ' + yuan(res.body.quote.quote_fen) + '（你确认的是 ' +
            amount + '），没有下单。请重新算价，核对后再确认。';
        } else if (res.status === 409 && e === 'bad_state') {
          ord.msg = '现在不能下单：还没截单，或者已经下过单了。看上面的状态。';
        } else {
          ord.msg = errText(res);
        }
      });
    }
    function doRecent() {
      orderRun('recent-orders', {}, function (res) {
        if (res.status === 200 && Array.isArray(res.body)) ord.recent = res.body;
        else ord.msg = errText(res);
      });
    }
    function doAdopt(o) {
      var amt = o.real_total_fen == null ? '金额未知' : yuan(o.real_total_fen);
      if (!confirm('确认订单 ' + o.order_id + '（' + amt + '）就是刚才下的这一单？')) return;
      orderRun('adopt', { order_id: o.order_id }, function (res) {
        if (res.status === 200) ord.recent = null; else ord.msg = errText(res);
      });
    }
    function doNotPlaced() {
      if (!confirm('确认麦当劳没有收到这一单？\n最近订单里没有，麦当劳 App「我的订单」里也没有，再点确定。\n回到截单后可以重新算价、下单。')) return;
      orderRun('not-placed', {}, function (res) {
        if (res.status === 200) ord.recent = null; else ord.msg = errText(res);
      });
    }
    function doRefreshStatus() {
      orderRun('refresh', {}, function (res) { if (res.status !== 200) ord.msg = errText(res); });
    }
    function doCancel() {
      var r = REASONS.filter(function (x) { return x[0] === ord.reason; })[0];
      if (!r) { ord.msg = ERRORS.reason_code; renderOrder(); return; }
      if (!confirm('确认取消订单 ' + data.order_id + '？\n原因：' + r[1])) return;
      orderRun('cancel', { reason_code: r[0] }, function (res) {
        if (res.status === 200) ord.reason = ''; else ord.msg = errText(res);
      });
    }

    function quoteBox(q) {
      var box = el('div', 'box');
      (q.lines || []).forEach(function (l) {
        box.appendChild(line(l.name + (l.qty > 1 ? ' ×' + l.qty : ''), l.subtotal_fen));
      });
      var bd = q.breakdown || {};
      box.appendChild(line('商品', bd.product_fen));
      box.appendChild(line('配送费', bd.delivery_fen));
      box.appendChild(line('打包费', bd.packing_fen));
      if (bd.discount_fen) {
        var d = el('div', 'row');
        d.appendChild(el('span', null, '优惠')); d.appendChild(el('span', null, '-' + yuan(bd.discount_fen)));
        box.appendChild(d);
      }
      box.appendChild(line('应付', q.quote_fen, true));
      if (bd.original_fen != null && bd.original_fen !== q.quote_fen) {
        box.appendChild(el('div', 'muted', '原价 ' + yuan(bd.original_fen)));
      }
      return box;
    }

    function couponList() {
      var wrap = el('div'); wrap.style.padding = '4px 0';
      wrap.appendChild(el('div', 'muted', '优惠券（选了会重新算价）'));
      function opt(label, c) {
        var l = el('label'); l.style.display = 'block'; l.style.padding = '2px 0';
        var r = el('input'); r.type = 'radio'; r.name = 'coupon'; r.disabled = ord.busy;
        r.checked = c ? !!ord.coupon && ord.coupon.id === c.id && ord.coupon.code === c.code : !ord.coupon;
        r.onchange = function () {
          ord.coupon = c ? { id: c.id, code: c.code } : null;
          ord.quote = null; ord.note = '';
          doQuote();
        };
        l.appendChild(r); l.appendChild(document.createTextNode(' ' + label));
        return l;
      }
      wrap.appendChild(opt('不用券', null));
      ord.coupons.forEach(function (c) { wrap.appendChild(opt(String(c.name || '优惠券'), c)); });
      return wrap;
    }

    function lockedView(box) {
      box.appendChild(el('b', null, '算价下单'));
      var tw = el('label'); tw.style.display = 'block'; tw.style.padding = '4px 0';
      var cb = el('input'); cb.type = 'checkbox'; cb.checked = ord.need; cb.disabled = ord.busy;
      cb.onchange = function () { ord.need = cb.checked; ord.quote = null; ord.note = ''; renderOrder(); };
      tw.appendChild(cb); tw.appendChild(document.createTextNode(' 需要餐具'));
      box.appendChild(tw);
      var rm = el('input'); rm.type = 'text'; rm.maxLength = 50; rm.value = ord.remark;
      rm.placeholder = '给麦当劳的订单备注（选填，最多 50 字）';
      rm.oninput = function () { ord.remark = rm.value; };
      box.appendChild(rm);
      if (ord.coupons.length) box.appendChild(couponList());
      box.appendChild(button('btn2', ord.quote ? '重新算价' : '算价', doQuote));
      if (ord.note) box.appendChild(el('div', 'banner', ord.note));
      if (ord.quote) {
        box.appendChild(quoteBox(ord.quote));
        box.appendChild(button('btn', '确认下单 ' + yuan(ord.quote.quote_fen), doOrder));
      }
    }
    function uncertainView(box) {
      box.appendChild(el('div', 'banner', '下单结果不明，先查最近订单'));
      box.appendChild(el('p', 'muted', '麦当劳没有及时回复，这一单可能已经下了。确认之前不能再下单，免得重复。'));
      if (data.quote && data.quote.quote_fen != null) {
        box.appendChild(el('div', null, '这次确认的金额：' + yuan(data.quote.quote_fen)));
      }
      box.appendChild(button('btn2', '查最近订单', doRecent));
      // always shown, but usable only after the server recorded a 查最近订单 attempt (spec 5.2)
      var np = button('btn2', '没下成，回到截单', doNotPlaced);
      if (!data.recent_checked) np.disabled = true;
      box.appendChild(np);
      if (!data.recent_checked) {
        box.appendChild(el('div', 'muted', '先点「查最近订单」；查不到也请去麦当劳 App「我的订单」确认'));
      }
      if (ord.recent) {
        if (!ord.recent.length) box.appendChild(el('p', 'muted', '最近没有订单'));
        ord.recent.forEach(function (o) {
          var row = el('div', 'entry');
          var what = el('div', 'what');
          what.appendChild(el('div', null, (o.create_time || '') + ' ' + (o.store_name || '')));
          what.appendChild(el('div', 'muted', (o.status_text || '') + ' · ' +
            (o.real_total_fen == null ? '金额未知' : yuan(o.real_total_fen)) + ' · ' + o.order_id));
          row.appendChild(what);
          row.appendChild(button('btn2', '就是这单', function () { doAdopt(o); }));
          box.appendChild(row);
        });
      }
    }
    function loadQr(img) {
      // the QR is a host API too, so it is fetched with the header and shown as a data: URL
      fetch('api/host/sessions/' + encodeURIComponent(sid) + '/pay-qr.svg',
        { headers: hostHeaders(false), credentials: 'same-origin' }).then(function (r) {
        if (r.status !== 200) throw new Error('qr ' + r.status);
        return r.text();
      }).then(function (svg) {
        img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
      }).catch(function () { img.alt = '二维码加载失败，请点「去支付」'; });
    }
    function orderedView(box) {
      box.appendChild(el('b', null, data.state === 'cancelled' ? '订单已取消' : '已下单'));
      box.appendChild(el('div', 'muted', '订单号 ' + (data.order_id || '')));
      if (data.order_status) box.appendChild(el('div', null, '状态：' + data.order_status));
      if (data.expect_delivery_time) box.appendChild(el('div', null, deliveryText(data.expect_delivery_time)));
      var q = data.quote || {};
      if (q.real_total_fen != null) box.appendChild(el('div', null, '麦当劳下单金额 ' + yuan(q.real_total_fen)));
      if (q.mismatch && q.real_total_fen == null) {
        box.appendChild(el('div', 'banner', '注意：麦当劳没有返回下单金额。' +
          (q.quote_fen != null ? '你确认的是 ' + yuan(q.quote_fen) + '，' : '') +
          '请在支付页核对金额后再支付，不对可以取消订单。'));
      } else if (q.mismatch) {
        box.appendChild(el('div', 'banner', '注意：麦当劳下单金额 ' + yuan(q.real_total_fen) + ' 和你确认的 ' +
          yuan(q.quote_fen) + ' 不一样。请核对后再支付，不对可以取消订单。'));
      }
      if (data.state === 'ordered') {
        var url = data.pay_url;
        if (typeof url === 'string' && url.indexOf('https://') === 0) {
          var payLink = el('a', 'btn', '去支付'); payLink.href = url;
          payLink.target = '_blank'; payLink.rel = 'noopener noreferrer';
          payLink.style.textAlign = 'center'; payLink.style.textDecoration = 'none';
          box.appendChild(payLink);
          var img = el('img', 'qr'); img.alt = '支付二维码'; img.width = 180; img.height = 180;
          loadQr(img);
          box.appendChild(img);
          box.appendChild(el('div', 'muted', '也可以用另一台手机扫码支付'));
        } else {
          box.appendChild(el('div', 'banner', '没有拿到支付链接，请去麦当劳 App 支付（订单号 ' +
            (data.order_id || '') + '）'));
        }
      }
      box.appendChild(button('btn2', '刷新状态', doRefreshStatus));
      if (data.state !== 'ordered') return;
      var c = el('div'); c.style.marginTop = '10px';
      var sel = el('select'); sel.disabled = ord.busy; sel.style.padding = '6px'; sel.style.marginRight = '8px';
      var o0 = el('option', null, '选择取消原因'); o0.value = ''; sel.appendChild(o0);
      REASONS.forEach(function (r) { var o = el('option', null, r[1]); o.value = r[0]; sel.appendChild(o); });
      sel.value = ord.reason;
      sel.onchange = function () { ord.reason = sel.value; };
      c.appendChild(sel);
      c.appendChild(button('btn2', '取消订单', doCancel));
      box.appendChild(c);
    }

    function renderOrder() {
      if (!data) return;
      if (ord.quote && (data.state !== 'locked' || ord.quoteFor !== entriesKey())) ord.quote = null;
      if (data.state !== 'locked') ord.note = '';
      if (data.state !== 'uncertain') ord.recent = null;
      // rebuild only when something visible changed, so typing in the remark box survives polling
      var key = JSON.stringify([data.state, data.quote, data.pay_url, data.order_id, data.order_status,
        data.expect_delivery_time, data.recent_checked, entriesKey(), ord.quote, ord.need, ord.coupon, ord.coupons, ord.busy, ord.msg, ord.note, ord.recent]);
      if (key === orderKey) return;
      orderKey = key;
      clear(elOrder);
      var box = el('div', 'box');
      if (data.state === 'open') box.appendChild(el('div', 'muted', '截单后可以在这里算价、下单'));
      else if (data.state === 'locked') lockedView(box);
      else if (data.state === 'ordering') box.appendChild(el('div', 'hot', '正在下单，请稍候…'));
      else if (data.state === 'uncertain') uncertainView(box);
      else orderedView(box);
      if (ord.msg) box.appendChild(el('div', 'err', ord.msg));
      elOrder.appendChild(box);
    }

    function renderAll() { renderHead(); renderAct(); renderList(); renderOrder(); }

    function act(which) {
      if (busy) return;
      busy = true; msg = ''; renderAct();
      api('POST', 'sessions/' + encodeURIComponent(sid) + '/' + which).then(function (res) {
        busy = false;
        if (res.status !== 200) { msg = errText(res); } else { headBuilt = false; }
        return refresh();
      }).catch(function () { busy = false; msg = '网络不太好，再试一次'; renderAct(); });
    }
    function refresh() {
      return api('GET', 'sessions/' + encodeURIComponent(sid)).then(function (res) {
        if (res.status !== 200) {
          if (!data) { clear(root); root.appendChild(el('p', 'err', errText(res))); }
          return;
        }
        data = res.body;
        if (!headBuilt) { renderAll(); headBuilt = true; } else { renderAct(); renderList(); renderStateOnly(); renderOrder(); }
      }).catch(function () { /* keep polling */ });
    }
    function renderStateOnly() {
      // keep the copy box (and its selection) stable; only refresh the status line
      var h = elHead.querySelector('.hot');
      if (h) {
        h.textContent = { open: '接龙中', locked: '已截单', ordering: '下单中', uncertain: '结果确认中',
          ordered: '已下单', cancelled: '已取消' }[data.state] || data.state;
      }
    }
    refresh();
    setInterval(refresh, 5000);
  }


  // Demo banner: the server says which mode it runs in (no auth, no MCP).
  fetch('api/meta', { headers: hostHeaders(false), credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (m) {
    if (!m || m.mode !== 'demo') return;
    banner('banner', '演示模式：数据是假的，不会真的下单');
  }).catch(function () { /* banner is best-effort */ });

  var LOGIN_ERRORS = { 401: '口令不对，请检查发起人链接', 429: '口令错误次数太多，稍后再试', 403: ERRORS.csrf };
  login().then(function (res) {
    if (res && res.status !== 200) banner('err', LOGIN_ERRORS[res.status] || ('登录失败（' + res.status + '）'));
  }, function () { banner('err', '网络不太好，登录失败，请刷新重试'); }).then(function () {
    if (sid) manageView(); else createView();
  });
})();
