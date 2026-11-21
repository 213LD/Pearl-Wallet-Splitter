(() => {
  const state = {
    data: null,
    currentView: "dashboard",
    workerFilter: "all",
    workerSearch: "",
    showAllRewards: false,
    settleWorkerFilter: "all",
    selectedPaymentIndices: new Set(),
    settleDraft: null,
    settleSplit: null,
    settlePrevCheckpointId: null,
    activeDraftId: null,
    activeDraftMeta: null,
    countdownDeadlineMs: 0,
    lastRenderedSec: -1,
    isLoadingState: false,
    pendingReloadWalletId: undefined,
    lastZeroSyncMs: 0,
    lmMode: "bat",
    lmLatestSeq: 0,
    lmLines: [],
    lmLastStatus: null,
    lmPolling: false,
    colSummary: null,
    colSelectedClientId: "",
    kernelInfo: null,
    kernelReleases: [],
    kernelLatestHtmlUrl: "",
    kernelTaskTimer: null,
    batLoadedOnce: false,
    notifications: [],
    notifFilter: "all",
    notifSearch: "",
    notifDrawerOpen: false,
    lastPollerErrNotifKey: "",
    selectedTradeIds: new Set(),
    linkedTradeIds: [],
    residualSource: "direct",
    onboardingSkipped: false,
    onboardingMembers: [],
    feeFormState: null,
  };

  const api = () => window.pywebview && window.pywebview.api;

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtInt(n) {
    return Number(n || 0).toLocaleString("zh-CN");
  }

  function fmtMoney(n) {
    return Number(n || 0).toFixed(2);
  }

  function fmtPrl(n) {
    return Number(n || 0).toFixed(6).replace(/\.?0+$/, (m) => (m.startsWith(".") ? "" : m));
  }

  function nowLocalStr() {
    const d = new Date();
    const pad = (x) => String(x).padStart(2, "0");
    return (
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
      `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    );
  }

  const NOTIF_STORAGE_KEY = "pearl_notifications_v1";
  const NOTIF_MAX_ITEMS = 150;

  function loadStoredNotifications() {
    try {
      const raw = localStorage.getItem(NOTIF_STORAGE_KEY);
      if (!raw) return [];
      const arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr.slice(0, NOTIF_MAX_ITEMS) : [];
    } catch (_) {
      return [];
    }
  }

  function persistNotifications() {
    try {
      localStorage.setItem(
        NOTIF_STORAGE_KEY,
        JSON.stringify((state.notifications || []).slice(0, NOTIF_MAX_ITEMS))
      );
    } catch (_) {}
  }

  function inferNotifCategory(msg, viewTarget = "") {
    const txt = String(msg || "");
    if (viewTarget === "kernel" || /内核|SRBMiner-MULTI|GitHub|\.bat/i.test(txt)) return "内核与脚本";
    if (viewTarget === "collector" || /集群|收集后端|上报包|Token/.test(txt)) return "集群后端";
    if (viewTarget === "settle" || /草稿|结算|分红|提现|基准|账本/.test(txt)) return "分红结算";
    if (/矿机|进程|PID|算力|日志/.test(txt)) return "矿机监控";
    if (/矿池|钱包|拉取|离线唤醒/.test(txt)) return "矿池同步";
    return "系统通知";
  }

  function renderNotifBadge() {
    const list = state.notifications || [];
    const unread = list.filter((n) => !n.read);
    const badge = document.getElementById("notif-unread-badge");
    if (badge) {
      badge.textContent = unread.length > 99 ? "99+" : String(unread.length);
      badge.classList.toggle("hidden", unread.length === 0);
      badge.classList.toggle("has-error", unread.some((n) => n.type === "error"));
    }
    const cnt = document.getElementById("notif-drawer-count");
    if (cnt) {
      cnt.textContent = unread.length > 0 ? `未读 ${unread.length} / 共 ${list.length} 条` : `共 ${list.length} 条`;
    }
  }

  function renderNotifDrawerList() {
    const box = document.getElementById("notif-list");
    if (!box) return;
    renderNotifBadge();
    const list = state.notifications || [];
    const filter = state.notifFilter || "all";
    const needle = String(state.notifSearch || "").trim().toLowerCase();

    const filtered = list.filter((item) => {
      if (filter === "unread" && item.read) return false;
      if (filter === "ok" && item.type !== "ok") return false;
      if (filter === "warn" && item.type !== "warn") return false;
      if (filter === "error" && item.type !== "error") return false;
      if (needle) {
        const hay = `${item.msg} ${item.category} ${item.ts}`.toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    });

    if (!filtered.length) {
      box.innerHTML = `<div class="empty-state">${
        list.length ? "当前筛选条件下没有匹配的通知记录" : "暂无通知记录"
      }</div>`;
      return;
    }

    const iconMap = { ok: "✅", warn: "⚠️", error: "❌" };
    box.innerHTML = filtered
      .map((item) => {
        const typeCls = item.type === "error" ? "error" : item.type === "warn" ? "warn" : "ok";
        const jumpBtn = item.viewTarget
          ? `<button class="link-btn" data-nact="jump" data-nid="${escapeHtml(item.id)}" data-view="${escapeHtml(item.viewTarget)}">前往页面 ↗</button>`
          : "";
        return `
          <div class="notif-card ${typeCls} ${item.read ? "" : "unread"}" data-nid="${escapeHtml(item.id)}">
            <div class="notif-card-top">
              <div class="notif-card-meta">
                ${item.read ? "" : '<span class="notif-unread-dot" title="未读"></span>'}
                <span>${iconMap[typeCls] || "🔔"}</span>
                <span class="notif-cat-tag">${escapeHtml(item.category || "系统通知")}</span>
                <span class="mono">${escapeHtml(item.ts || "")}</span>
              </div>
              <button class="toast-close" data-nact="delete" data-nid="${escapeHtml(item.id)}" title="删除此条">&times;</button>
            </div>
            <div class="notif-card-msg">${escapeHtml(item.msg)}</div>
            <div class="notif-card-actions">
              ${item.read ? "" : `<button class="link-btn" data-nact="read" data-nid="${escapeHtml(item.id)}">标为已读</button>`}
              ${jumpBtn}
              <button class="link-btn" data-nact="copy" data-nid="${escapeHtml(item.id)}">📋 复制内容</button>
            </div>
          </div>
        `;
      })
      .join("");
  }

  function openNotifDrawer(markReadAll = false) {
    state.notifDrawerOpen = true;
    if (markReadAll) {
      state.notifications.forEach((n) => {
        n.read = true;
      });
      persistNotifications();
    }
    document.getElementById("notif-drawer-backdrop")?.classList.remove("hidden");
    renderNotifDrawerList();
  }

  function closeNotifDrawer() {
    state.notifDrawerOpen = false;
    document.getElementById("notif-drawer-backdrop")?.classList.add("hidden");
    renderNotifBadge();
  }

  function addNotification(msg, type = "ok", opts = {}) {
    const cleanMsg = String(msg ?? "").trim();
    if (!cleanMsg) return null;
    const item = {
      id: `n-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      ts: nowLocalStr(),
      type: type === "error" || type === "warn" ? type : "ok",
      category: opts.category || inferNotifCategory(cleanMsg, opts.viewTarget || state.currentView),
      msg: cleanMsg,
      read: Boolean(state.notifDrawerOpen),
      viewTarget: opts.viewTarget || "",
    };
    state.notifications = [item, ...(state.notifications || [])].slice(0, NOTIF_MAX_ITEMS);
    persistNotifications();
    renderNotifBadge();
    if (state.notifDrawerOpen) {
      renderNotifDrawerList();
    }
    return item;
  }

  function toast(msg, type = "ok", opts = {}) {
    const cleanMsg = String(msg ?? "");
    if (!opts.skipCenter) {
      addNotification(cleanMsg, type, opts);
    }

    const box = document.getElementById("toast-container");
    if (!box) return;
    while (box.children.length >= 4) {
      box.firstElementChild?.remove();
    }

    const el = document.createElement("div");
    el.className = `toast ${type === "error" ? "error" : type === "warn" ? "warn" : ""}`;
    el.title = "鼠标悬停时暂停自动关闭";

    const top = document.createElement("div");
    top.className = "toast-top";

    const textEl = document.createElement("div");
    textEl.className = "toast-msg";
    textEl.textContent = cleanMsg;

    const closeBtn = document.createElement("button");
    closeBtn.className = "toast-close";
    closeBtn.type = "button";
    closeBtn.innerHTML = "&times;";
    closeBtn.title = "关闭提示（通知中心仍保留记录）";

    top.appendChild(textEl);
    top.appendChild(closeBtn);

    const foot = document.createElement("div");
    foot.className = "toast-foot";
    const hintEl = document.createElement("span");
    hintEl.textContent = `${nowLocalStr().slice(11)} · 悬停暂停`;
    const openBtn = document.createElement("button");
    openBtn.className = "toast-open-center";
    openBtn.type = "button";
    openBtn.textContent = "通知中心 ↗";
    foot.appendChild(hintEl);
    foot.appendChild(openBtn);

    el.appendChild(top);
    el.appendChild(foot);
    box.appendChild(el);

    const defaultDuration = type === "error" ? 14000 : type === "warn" ? 10500 : 7500;
    let remaining = Number(opts.duration || defaultDuration);
    let timerId = null;
    let startAt = 0;

    const removeToast = () => {
      if (timerId) clearTimeout(timerId);
      el.remove();
    };
    const startTimer = () => {
      if (opts.sticky) return;
      if (timerId) clearTimeout(timerId);
      startAt = Date.now();
      timerId = setTimeout(removeToast, Math.max(2500, remaining));
    };
    const pauseTimer = () => {
      if (timerId) {
        clearTimeout(timerId);
        timerId = null;
        remaining = Math.max(2500, remaining - (Date.now() - startAt));
      }
    };

    closeBtn.onclick = removeToast;
    openBtn.onclick = () => {
      removeToast();
      openNotifDrawer(false);
    };
    el.addEventListener("mouseenter", pauseTimer);
    el.addEventListener("mouseleave", startTimer);
    startTimer();
  }

  function playChime() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(880, ctx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(1318.5, ctx.currentTime + 0.14);
      gain.gain.setValueAtTime(0.12, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.36);
    } catch (_) {}
  }

  async function copyText(text, successMsg = "已复制到剪贴板", toastOpts = {}) {
    try {
      await navigator.clipboard.writeText(text);
      toast(successMsg, "ok", toastOpts);
    } catch (_) {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      toast(successMsg, "ok", toastOpts);
    }
  }

  // -------------------------------------------------------------------
  // Modal Helper
  // -------------------------------------------------------------------
  function openModal(title, bodyHtml, footerButtons, wide = false) {
    document.getElementById("modal-card").classList.toggle("modal-wide", Boolean(wide));
    document.getElementById("modal-title").textContent = title;
    document.getElementById("modal-body").innerHTML = bodyHtml;
    const footer = document.getElementById("modal-footer");
    footer.innerHTML = "";
    footerButtons.forEach((btnCfg) => {
      const b = document.createElement("button");
      b.className = `btn ${btnCfg.cls || "btn-secondary"}`;
      b.textContent = btnCfg.text;
      b.onclick = btnCfg.onClick;
      footer.appendChild(b);
    });
    document.getElementById("modal-backdrop").classList.remove("hidden");
  }

  function closeModal() {
    document.getElementById("modal-backdrop").classList.add("hidden");
  }

  function confirmDialog(message, opts = {}) {
    return new Promise((resolve) => {
      const backdrop = document.getElementById("confirm-backdrop");
      const titleEl = document.getElementById("confirm-title");
      const msgEl = document.getElementById("confirm-message");
      const okBtn = document.getElementById("confirm-ok");
      const cancelBtn = document.getElementById("confirm-cancel");
      const closeBtn = document.getElementById("confirm-close");

      titleEl.textContent = opts.title || "请确认操作";
      msgEl.textContent = message;
      okBtn.textContent = opts.confirmText || "确定";
      cancelBtn.textContent = opts.cancelText || "取消";
      okBtn.className = `btn ${opts.danger ? "btn-danger" : "btn-primary"}`;

      const finish = (val) => {
        backdrop.classList.add("hidden");
        resolve(val);
      };

      okBtn.onclick = () => finish(true);
      cancelBtn.onclick = () => finish(false);
      closeBtn.onclick = () => finish(false);
      backdrop.onclick = (e) => {
        if (e.target.id === "confirm-backdrop") finish(false);
      };

      backdrop.classList.remove("hidden");
      okBtn.focus();
    });
  }

  // -------------------------------------------------------------------
  // State Loading & Event Processing
  // -------------------------------------------------------------------
  function applyStatePayload(next) {
    if (!next) return;
    state.data = next;
    const remMs = Number((next.poller && next.poller.remaining_ms) ?? 0);
    state.countdownDeadlineMs = remMs > 0 ? Date.now() + remMs : 0;
    state.lastRenderedSec = -1;
    handlePollerEvents(next.events || []);

    const p = next.poller || {};
    if (p.last_error && p.last_error_at) {
      const errKey = `${p.last_error_at}|${p.last_error}`;
      if (state.lastPollerErrNotifKey !== errKey) {
        state.lastPollerErrNotifKey = errKey;
        addNotification(`矿池连接异常：${p.last_error}`, "error", {
          category: "矿池同步",
          viewTarget: "dashboard",
        });
      }
    }
    renderAll();
    maybeSyncOnboardingModal();
  }

  async function loadState(walletId = null) {
    if (!api()) return;
    if (state.isLoadingState) {
      state.pendingReloadWalletId = walletId;
      return;
    }
    state.isLoadingState = true;
    try {
      const next = await api().get_state(walletId);
      applyStatePayload(next);
    } catch (err) {
      toast(`加载状态失败: ${err}`, "error");
    } finally {
      state.isLoadingState = false;
      if (state.pendingReloadWalletId !== undefined) {
        const queuedId = state.pendingReloadWalletId;
        state.pendingReloadWalletId = undefined;
        loadState(queuedId);
      }
    }
  }

  function handlePollerEvents(events) {
    events.forEach((ev) => {
      if (ev.type === "worker_woken" && ev.workers && ev.workers.length) {
        playChime();
        const names = ev.workers.map((w) => `${w.worker_name}(${fmtInt(w.new_shares)})`).join("、");
        toast(`已捕获上线矿机最新份额：${names}！现在可以关机了`, "ok", {
          category: "离线唤醒",
          viewTarget: "dashboard",
          duration: 15000,
        });
      } else if (ev.type === "worker_offline_alert") {
        const names = (ev.workers || []).map((w) => w.worker_name).join("、");
        toast(`已发送微信掉线告警：${names}（仅提醒一次直到恢复上线）`, "warn", {
          category: "掉线告警",
          viewTarget: "alerts",
          duration: 15000,
        });
      } else if (ev.type === "worker_recovery_alert") {
        const names = (ev.workers || []).map((w) => w.worker_name).join("、");
        toast(`矿机已恢复上线并重置告警状态：${names}`, "ok", {
          category: "恢复上线",
          viewTarget: "alerts",
          duration: 10000,
        });
      } else if (ev.type === "poll_fail_alert") {
        toast(`${ev.title || "矿池连续拉取失败超阈值，已推送群提醒"}`, "error", {
          category: "轮询超时",
          viewTarget: "alerts",
          duration: 15000,
        });
      } else if (ev.type === "alert_push_error") {
        toast(`微信告警发送失败：${ev.error || "请检查网络或 Token 配置"}`, "error", {
          category: "推送失败",
          viewTarget: "alerts",
          duration: 12000,
        });
      }
    });
  }

  // -------------------------------------------------------------------
  // Rendering
  // -------------------------------------------------------------------
  function renderAll() {
    if (!state.data) return;
    renderSidebar();
    renderTopbar();
    renderBanners();
    renderDashboard();
    renderBlockRewards();
    syncLocalMinerPrefs();
    renderCollectorPrefs();
    renderAlertsView();
    renderSettleDraftsPanel();
    renderLedger();
    renderMembers();
    renderSettings();
    if (state.currentView === "settle") {
      if (!state.settleDraft) {
        initSettleDraft();
      } else {
        const ae = document.activeElement;
        const editingTable = ae && ae.closest && ae.closest("#settle-workers-tbody");
        if (!editingTable) {
          recomputeSettlePreview();
        }
      }
    }
  }

  function renderSidebar() {
    const { wallets, active_wallet, settings, poller } = state.data;
    const list = document.getElementById("wallet-list");
    if (!wallets || !wallets.length) {
      list.innerHTML = `<div class="empty-state" style="padding:10px 6px;font-size:12px;">暂未绑定钱包地址</div>`;
    } else {
      list.innerHTML = wallets
        .map((w) => {
          const isActive = active_wallet && w.id === active_wallet.id;
          const shortAddr = `${w.address.slice(0, 7)}...${w.address.slice(-5)}`;
          const isEx = w.wallet_type === "exchange";
          const typeTag = isEx
            ? `<span class="wallet-type-tag exchange" title="交易所钱包：矿池直充交易所">交易所</span>`
            : `<span class="wallet-type-tag official" title="Pearl 官方钱包：转入交易所可填转账费">官方</span>`;
          return `
            <div class="wallet-item ${isActive ? "active" : ""}" data-wallet-id="${w.id}">
              <div class="wallet-item-top">
                <span class="wallet-alias">${escapeHtml(w.alias)}</span>
                <span class="wallet-count-pill" title="在线 / 总缓存">${w.cached_online}/${w.cached_total}</span>
              </div>
              <div class="wallet-addr-short" style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
                <span>${escapeHtml(shortAddr)}</span>
                ${typeTag}
              </div>
            </div>
          `;
        })
        .join("");
    }

    list.querySelectorAll(".wallet-item").forEach((el) => {
      el.onclick = async () => {
        const id = Number(el.dataset.walletId);
        state.settleDraft = null;
        state.activeDraftId = null;
        state.activeDraftMeta = null;
        state.settlePrevCheckpointId = null;
        state.selectedPaymentIndices.clear();
        clearFeeFormPayload();
        await loadState(id);
      };
    });

    const draftsBadge = document.getElementById("nav-drafts-badge");
    if (draftsBadge) {
      const draftCount = (state.data.drafts || []).length;
      draftsBadge.textContent = `草稿 ${draftCount}`;
      draftsBadge.classList.toggle("hidden", draftCount === 0);
    }

    renderPollerCountdowns();
  }

  function renderPollerCountdowns(force = true) {
    if (!state.data) return;
    const { active_wallet, settings, poller } = state.data;
    const remMs = Math.max(0, state.countdownDeadlineMs - Date.now());
    const rem = remMs > 0 ? Math.ceil(remMs / 1000) : 0;
    const autoOn = settings && settings.auto_poll_enabled === "1";
    const isFetching = Boolean(poller && poller.is_fetching);
    const isWatchingAny = Boolean(poller && poller.watch_mode);
    const isWatchingThis = Boolean(
      isWatchingAny && active_wallet && poller.watch_wallet_id === active_wallet.id
    );
    const walletSt =
      (active_wallet && poller && poller.wallet_status && poller.wallet_status[String(active_wallet.id)]) ||
      null;
    const hasConnError = Boolean(
      !isFetching &&
        ((walletSt && walletSt.state === "error") || (!walletSt && poller && poller.last_error))
    );
    const errText = (walletSt && walletSt.error) || (poller && poller.last_error) || "";

    if (!force && rem === state.lastRenderedSec) {
      return { rem, remMs, isFetching, isWatchingAny, autoOn, active_wallet };
    }
    state.lastRenderedSec = rem;

    const pill = document.getElementById("sidebar-poller-status");
    if (hasConnError) {
      const retryTxt = isWatchingAny || autoOn ? `${rem}s后重试` : "连接异常";
      pill.innerHTML = `<span class="dot dot-err"></span><span style="color:#fca5a5">矿池连接失败 (${retryTxt})</span>`;
    } else if (isWatchingAny) {
      const label = isFetching || rem === 0 ? "极速唤醒监听中 (拉取中...)" : `极速唤醒监听中 (${rem}s)`;
      pill.innerHTML = `<span class="dot dot-warn"></span><span>${label}</span>`;
    } else if (autoOn) {
      const label = isFetching || rem === 0 ? "自动挂机缓存 (同步中...)" : `自动挂机缓存 (${rem}s)`;
      pill.innerHTML = `<span class="dot dot-ok"></span><span>${label}</span>`;
    } else {
      pill.innerHTML = `<span class="dot dot-paused"></span><span>自动拉取已暂停</span>`;
    }

    if (active_wallet) {
      // Topbar connection badge
      const connBadge = document.getElementById("pool-conn-badge");
      const connText = document.getElementById("pool-conn-text");
      if (connBadge && connText) {
        if (isFetching) {
          connBadge.className = "conn-badge conn-syncing";
          connText.textContent = "正在连接矿池...";
          connBadge.title = "正在从 ntminerpool.com 拉取最新矿机与份额数据";
        } else if (hasConnError) {
          connBadge.className = "conn-badge conn-error";
          connText.textContent = "矿池连接失败";
          connBadge.title = `连接失败：${errText}`;
        } else if (active_wallet.last_fetched_at) {
          connBadge.className = "conn-badge conn-ok";
          connText.textContent = "矿池连接正常";
          connBadge.title = `与 ntminerpool.com 通信正常（最近成功：${active_wallet.last_fetched_at}）`;
        } else {
          connBadge.className = "conn-badge conn-idle";
          connText.textContent = "尚未连接";
          connBadge.title = "尚未拉取矿池数据";
        }
      }

      const syncBase = active_wallet.last_fetched_at
        ? `最后同步: ${active_wallet.last_fetched_at}`
        : "尚未同步";
      let nextTxt = "";
      if (isFetching || (rem === 0 && (isWatchingAny || autoOn))) {
        nextTxt = " · 正在同步...";
      } else if (hasConnError) {
        nextTxt = isWatchingAny || autoOn ? ` · 连接异常 (${rem}s后重试)` : " · 连接异常";
      } else if (isWatchingAny) {
        nextTxt = ` · 唤醒监听 ${rem}s`;
      } else if (autoOn) {
        nextTxt = ` · 下次缓存 ${rem}s`;
      } else {
        nextTxt = " · 自动拉取已暂停";
      }
      document.getElementById("last-fetched-label").textContent = `${syncBase}${nextTxt}`;

      const watchBtn = document.getElementById("btn-toggle-watch");
      const watchText = document.getElementById("watch-btn-text");
      watchBtn.classList.toggle("active", isWatchingThis);
      if (isWatchingThis) {
        watchText.textContent =
          isFetching || rem === 0
            ? "监听中 (拉取中...) · 点击停止"
            : `监听中 (${rem}s) · 点击停止`;
      } else {
        watchText.textContent = "离线唤醒监听";
      }

      const bannerCd = document.getElementById("watch-banner-countdown");
      if (bannerCd) {
        bannerCd.textContent =
          isFetching || rem === 0 ? "正在拉取最新份额..." : `${rem}s 后自动拉取`;
      }
    }
    return { rem, remMs, isFetching, isWatchingAny, autoOn, active_wallet };
  }

  function tickCountdown() {
    const info = renderPollerCountdowns(false);
    if (!info) return;
    if (
      info.rem === 0 &&
      !info.isFetching &&
      (info.isWatchingAny || info.autoOn) &&
      Date.now() - state.countdownDeadlineMs > 1500 &&
      Date.now() - state.lastZeroSyncMs > 2500
    ) {
      state.lastZeroSyncMs = Date.now();
      loadState(info.active_wallet ? info.active_wallet.id : null);
    }
  }

  function renderTopbar() {
    const { active_wallet } = state.data;
    const typeBadge = document.getElementById("active-wallet-type-badge");
    if (!active_wallet) {
      document.getElementById("active-wallet-alias").textContent = "未绑定奈特矿池钱包";
      document.getElementById("active-wallet-address").textContent = "请点击「打开首次配置向导」或左上角 + 号添加 ntminerpool PRL 钱包";
      if (typeBadge) {
        typeBadge.textContent = "待配置";
        typeBadge.className = "wallet-type-tag official";
      }
      syncFeeWalletTypeLock();
      renderPollerCountdowns();
      return;
    }
    document.getElementById("active-wallet-alias").textContent = active_wallet.alias;
    if (typeBadge) {
      const isEx = active_wallet.wallet_type === "exchange";
      typeBadge.textContent = isEx ? "交易所钱包" : "官方钱包";
      typeBadge.className = `wallet-type-tag ${isEx ? "exchange" : "official"}`;
      typeBadge.title = isEx
        ? "交易所直充钱包（结算时标记「仅官方钱包」的转币手续费将自动免除）"
        : "Pearl 官方钱包（结算时可记录「仅官方钱包」转入交易所转账费）";
    }
    document.getElementById("active-wallet-address").textContent = active_wallet.address;
    syncFeeWalletTypeLock();
    renderPollerCountdowns();
  }

  function renderBanners() {
    const { poller, active_wallet, unassigned_workers } = state.data;
    const watchBanner = document.getElementById("watch-banner");
    const isWatchingThis = poller.watch_mode && active_wallet && poller.watch_wallet_id === active_wallet.id;
    watchBanner.classList.toggle("hidden", !isWatchingThis);

    // Pool connection error banner
    const errBanner = document.getElementById("pool-error-banner");
    if (errBanner) {
      const walletSt =
        (active_wallet && poller && poller.wallet_status && poller.wallet_status[String(active_wallet.id)]) ||
        null;
      const hasErr = Boolean(
        !poller.is_fetching &&
          ((walletSt && walletSt.state === "error") || (!walletSt && poller && poller.last_error))
      );
      if (hasErr) {
        const errMsg = (walletSt && walletSt.error) || poller.last_error || "未知网络错误";
        const errAt = (walletSt && walletSt.updated_at) || poller.last_error_at || "";
        document.getElementById("pool-error-detail").textContent =
          `${errMsg}${errAt ? `（发生于 ${errAt}）` : ""} — 当前面板展示本地已缓存的矿机数据。`;
        errBanner.classList.remove("hidden");
      } else {
        errBanner.classList.add("hidden");
      }
    }

    const uBanner = document.getElementById("unassigned-banner");
    if (unassigned_workers && unassigned_workers.length > 0) {
      uBanner.classList.remove("hidden");
      document.getElementById("unassigned-count").textContent = unassigned_workers.length;
      document.getElementById("unassigned-names-preview").textContent =
        unassigned_workers.map((u) => u.worker_name).slice(0, 4).join("、") +
        (unassigned_workers.length > 4 ? " 等" : "");
    } else {
      uBanner.classList.add("hidden");
    }
  }

  function renderDashboard() {
    const { active_wallet, member_live_summary, latest_baseline_info, round_pending_info, workers, checkpoints } = state.data;
    const emptyCta = document.getElementById("empty-wallet-cta");
    if (emptyCta) emptyCta.classList.toggle("hidden", Boolean(active_wallet));
    if (!active_wallet) {
      document.getElementById("stat-live-hash").textContent = "--";
      document.getElementById("stat-avg-hash").textContent = "24h 平均: --";
      document.getElementById("stat-worker-counts").textContent = "0 / 0";
      document.getElementById("stat-pool-offline-note").textContent = "请先通过配置向导绑定钱包";
      document.getElementById("stat-prl-price").textContent = "--";
      document.getElementById("stat-daily-yield").textContent = "预估日产: -- · 24h奖励: --";
      document.getElementById("stat-unpaid-prl").textContent = "0.0000 PRL";
      document.getElementById("stat-immature-prl").textContent = "未支付 0.0000 + 待成熟 0.0000 PRL";
      document.getElementById("stat-total-paid").textContent = "0.0000 PRL";
      document.getElementById("stat-settled-total").textContent = "￥0.00";
      document.getElementById("dashboard-round-pending-bar")?.classList.add("hidden");
      document.getElementById("member-live-cards").innerHTML = `<div class="empty-state">尚未添加奈特矿池钱包，完成首次配置向导后即可实时显示成员份额</div>`;
      document.getElementById("workers-tbody").innerHTML = `<tr><td colspan="10" class="empty-state">尚未添加矿池钱包，点击上方「打开首次配置向导」开始使用</td></tr>`;
      return;
    }
    const ov = (active_wallet && active_wallet.overview) || {};

    document.getElementById("stat-live-hash").textContent = ov.live_hashrate || "--";
    document.getElementById("stat-avg-hash").textContent = `24h 平均: ${ov.avg_24h_hashrate || "--"}`;

    const onlineCnt = workers.filter((w) => w.is_currently_online).length;
    const offlineCnt = workers.filter((w) => !w.is_currently_online).length;
    document.getElementById("stat-worker-counts").textContent = `${onlineCnt} / ${offlineCnt}`;
    document.getElementById("stat-pool-offline-note").textContent =
      offlineCnt > 0
        ? `矿池离线页仅显示 ${ov.offline_count ?? 0}，本地已缓存 ${offlineCnt} 台`
        : `矿池在线 ${ov.online_count ?? onlineCnt} · 本地已持久化`;

    const prlPrice = ov.prl_price_cny != null ? Number(ov.prl_price_cny) : 0;
    document.getElementById("stat-prl-price").textContent =
      ov.prl_price_cny != null ? `￥${Number(ov.prl_price_cny).toFixed(2)}` : "--";
    const estDailyPrl = Number(ov.est_daily_prl || 0);
    const rew24hPrl = Number(ov.reward_24h_prl || 0);
    const dailyEl = document.getElementById("stat-daily-yield");
    if (dailyEl) {
      if (estDailyPrl > 0 || rew24hPrl > 0) {
        const dailyCny = prlPrice > 0 && estDailyPrl > 0 ? ` (≈￥${fmtMoney(estDailyPrl * prlPrice)})` : "";
        const rewCny = prlPrice > 0 && rew24hPrl > 0 ? ` (≈￥${fmtMoney(rew24hPrl * prlPrice)})` : "";
        dailyEl.textContent = `预估日产 ${estDailyPrl.toFixed(4)} PRL${dailyCny} · 24h ${rew24hPrl.toFixed(4)} PRL${rewCny}`;
      } else {
        dailyEl.textContent = "预估日产: -- · 24h奖励: --";
      }
    }

    const unpaidPrl = Number(ov.unpaid_prl || 0);
    const immaturePrl = Number(ov.immature_prl || 0);
    const totalPendingPrl = unpaidPrl + immaturePrl;
    const pendingCnyHint = prlPrice > 0 ? ` (≈￥${(totalPendingPrl * prlPrice).toFixed(2)})` : "";
    document.getElementById("stat-unpaid-prl").textContent = `${totalPendingPrl.toFixed(4)} PRL${pendingCnyHint}`;
    document.getElementById("stat-immature-prl").textContent =
      `未支付 ${unpaidPrl.toFixed(4)} + 待成熟 ${immaturePrl.toFixed(4)} PRL`;

    const paymentsArr = Array.isArray(ov.payments) ? ov.payments : [];
    const paymentsSumPrl = paymentsArr.reduce((s, p) => s + Number(p.amount_prl || 0), 0);
    const rawPaidPrl = Number(ov.total_paid_prl || 0);
    const totalPaidPrl = rawPaidPrl > 0 ? rawPaidPrl : paymentsSumPrl;
    const paidCnyHint = prlPrice > 0 && totalPaidPrl > 0 ? ` (≈￥${fmtMoney(totalPaidPrl * prlPrice)})` : "";
    document.getElementById("stat-total-paid").textContent = `${totalPaidPrl.toFixed(4)} PRL${paidCnyHint}`;
    const paidSubEl = document.getElementById("stat-total-paid-sub");
    if (paidSubEl) {
      if (paymentsArr.length > 0) {
        const latestAt = String(paymentsArr[0].paid_at || "").trim();
        const shortAt = latestAt.length > 16 ? latestAt.slice(0, 16) : latestAt;
        paidSubEl.textContent = `已收录 ${paymentsArr.length} 笔转出${shortAt ? ` · 最近 ${shortAt}` : ""}`;
      } else if (totalPaidPrl > 0 && prlPrice > 0) {
        paidSubEl.textContent = `按现价折合约 ￥${fmtMoney(totalPaidPrl * prlPrice)} · 来自矿池官网统计`;
      } else {
        paidSubEl.textContent = "来自矿池 Dashboard 累计已支付统计";
      }
    }

    const settledList = (checkpoints || []).filter((c) => c.kind === "withdrawal");
    const settledCnyTotal = settledList.reduce((s, c) => s + Number(c.cny_amount || 0), 0);
    const settledPrlTotal = settledList.reduce((s, c) => s + Number(c.prl_amount || 0), 0);
    const settledMainEl = document.getElementById("stat-settled-total");
    const settledSubEl = document.getElementById("stat-settled-sub");
    if (settledMainEl && settledSubEl) {
      if (settledList.length > 0) {
        settledMainEl.textContent =
          settledCnyTotal > 0 || settledPrlTotal <= 0
            ? `￥${fmtMoney(settledCnyTotal)}`
            : `${settledPrlTotal.toFixed(4)} PRL`;
        const latestDate = String(settledList[0].checkpoint_time || "").slice(0, 10);
        const prlNote = settledPrlTotal > 0 ? ` · 共 ${settledPrlTotal.toFixed(4)} PRL` : "";
        settledSubEl.textContent =
          `已登记 ${settledList.length} 次提现分红${prlNote}${latestDate ? ` · 最近 ${latestDate}` : ""}`;
      } else {
        settledMainEl.textContent = "￥0.00";
        settledSubEl.textContent = "暂未登记提现记录 · 可在「提现与分红」入账";
      }
    }

    // Baseline badge in dashboard header
    const baseTag = document.getElementById("dashboard-baseline-tag");
    if (baseTag && latest_baseline_info) {
      if (latest_baseline_info.id) {
        const lbl =
          latest_baseline_info.label ||
          (latest_baseline_info.kind === "baseline" ? "基准起点" : "上次提现结算");
        baseTag.textContent = `本轮起点: ${latest_baseline_info.checkpoint_time} (${lbl}) · 本轮总增量 +${fmtInt(latest_baseline_info.total_round_delta)}`;
        baseTag.className = "status-badge status-online";
      } else {
        baseTag.textContent = `本轮起点: 从 0 份额起算 · 累计总份额 ${fmtInt(latest_baseline_info.total_all_shares)}`;
        baseTag.className = "status-badge status-manual";
      }
    }

    // Round Pending Summary Bar (last withdrawal/baseline -> next settlement)
    const rpBar = document.getElementById("dashboard-round-pending-bar");
    const rp = round_pending_info || null;
    if (rpBar && rp) {
      const hasContent = rp.round_est_total_prl > 0 || rp.pool_total_paid_prl > 0 || rp.checkpoint_id;
      rpBar.classList.toggle("hidden", !hasContent);
      if (hasContent) {
        const cnyStr = rp.round_est_total_cny > 0 ? ` (≈￥${fmtMoney(rp.round_est_total_cny)})` : "";
        let srcDesc = "";
        if (rp.source === "from_genesis") {
          srcDesc = "当前尚未登记过提现/基准，按矿池累计已付 + 当前待发估算";
        } else if (rp.source === "snapshot") {
          srcDesc = `矿池总已付 ${Number(rp.pool_total_paid_prl).toFixed(4)} − 上次快照 ${Number(rp.baseline_pool_paid_prl || 0).toFixed(4)} PRL`;
        } else if (rp.source === "payments_after_cp") {
          srcDesc = "已对齐上次基准后的矿池打款记录";
        } else if (rp.source === "total_minus_withdrawals") {
          srcDesc = `矿池总已付 ${Number(rp.pool_total_paid_prl).toFixed(4)} − 历史已提现汇总`;
        } else {
          srcDesc = `<span class="warn-tag">⚠️ 按最近矿池转账推算（若中途跨越多次5 PRL自动打款，请点右上角「校准已付起点」）</span>`;
        }
        rpBar.innerHTML = `
          <div>
            💰 ${rp.checkpoint_id ? "<strong>本轮下一次提现待发总预估池：" : "<strong>累计总收益预估池："}${Number(rp.round_est_total_prl || 0).toFixed(4)} PRL</strong>${escapeHtml(cnyStr)}
            ＝ 已打款未结算 <strong style="color:var(--primary)">${Number(rp.paid_since_baseline_prl || 0).toFixed(4)} PRL</strong> ＋ 矿池未发/待成熟 <strong style="color:var(--amber)">${Number(rp.pool_pending_prl || 0).toFixed(4)} PRL</strong>
          </div>
          <div style="font-size:11.5px;">${srcDesc}</div>
        `;
      }
    }

    // Member summary cards
    const cardsBox = document.getElementById("member-live-cards");
    cardsBox.innerHTML = member_live_summary
      .map((m) => {
        const pct = ((m.round_ratio || 0) * 100).toFixed(1);
        const estCnyStr = m.est_unpaid_cny > 0 ? ` (≈￥${fmtMoney(m.est_unpaid_cny)})` : "";
        const paidPart = Number(m.est_paid_pending_prl || 0).toFixed(4);
        const poolPart = Number(m.est_pool_pending_prl || 0).toFixed(4);
        const splitTip = `按本轮增量占比(${pct}%)估算：已打款未结 ${paidPart} PRL + 矿池当前待发 ${poolPart} PRL`;
        return `
        <div class="member-mini-card" style="border-top-color: ${escapeHtml(m.member_color)}">
          <div class="member-mini-top">
            <span class="member-mini-name">
              <span class="member-dot" style="background:${escapeHtml(m.member_color)}"></span>
              ${escapeHtml(m.member_name)}
            </span>
            <span class="wallet-count-pill">在线 ${m.online_count} · 离线 ${m.offline_cached_count}</span>
          </div>
          <div class="member-mini-main">
            <div>
              <div class="member-mini-shares">+${fmtInt(m.round_delta_shares ?? m.total_shares)}</div>
              <div style="font-size:11px;color:var(--text-dim);">本轮新增有效份额</div>
            </div>
            <div class="member-mini-ratio" style="color:${escapeHtml(m.member_color)}">${pct}%</div>
          </div>
          <div class="mini-progress-track">
            <div class="mini-progress-fill" style="width:${pct}%;background:${escapeHtml(m.member_color)}"></div>
          </div>
          <div class="member-mini-meta">
            <span>⚡ ${escapeHtml(m.live_hashrate_text || "0.00 TH/s")}</span>
            <span>累计: ${fmtInt(m.total_shares)}</span>
          </div>
          <div class="member-mini-sub" title="${escapeHtml(splitTip)}">
            <span>本轮待发预估</span>
            <strong style="color:var(--text)">${Number(m.est_unpaid_prl || 0).toFixed(4)} PRL${escapeHtml(estCnyStr)}</strong>
          </div>
        </div>
      `;
      })
      .join("");

    // Workers table
    const q = state.workerSearch.trim().toLowerCase();
    const filtered = workers.filter((w) => {
      if (state.workerFilter === "online" && !w.is_currently_online) return false;
      if (state.workerFilter === "offline" && w.is_currently_online) return false;
      if (state.workerFilter === "unassigned" && w.member_id !== null) return false;
      if (q) {
        return (
          w.worker_name.toLowerCase().includes(q) ||
          w.member_name.toLowerCase().includes(q)
        );
      }
      return true;
    });

    const tbody = document.getElementById("workers-tbody");
    if (!filtered.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="empty-state">暂无符合条件的矿机数据，点击右上角「立即拉取」或「手动录入离线矿机」</td></tr>`;
      return;
    }

    tbody.innerHTML = filtered
      .map((w) => {
        let badge = `<span class="status-badge status-online">● 在线</span>`;
        if (!w.is_currently_online) {
          badge =
            w.source === "manual"
              ? `<span class="status-badge status-manual">✎ 离线(手动)</span>`
              : `<span class="status-badge status-offline">◔ 离线(已缓存)</span>`;
        }
        const deltaVal = Number(w.round_delta_shares ?? w.last_shares);
        const deltaCls = deltaVal > 0 ? "delta-pos" : "delta-zero";
        return `
          <tr>
            <td><strong>${escapeHtml(w.worker_name)}</strong></td>
            <td>
              <span class="member-tag">
                <span class="member-dot" style="background:${escapeHtml(w.member_color)}"></span>
                ${escapeHtml(w.member_name)}
              </span>
            </td>
            <td>${badge}</td>
            <td class="num">${escapeHtml(w.current_hashrate)}</td>
            <td class="num">${escapeHtml(w.avg_24h_hashrate)}</td>
            <td class="num" style="color:var(--text-muted)">${fmtInt(w.baseline_shares || 0)}</td>
            <td class="num"><strong>${fmtInt(w.last_shares)}</strong></td>
            <td class="num"><span class="${deltaCls}">+${fmtInt(deltaVal)}</span></td>
            <td>${escapeHtml(w.last_seen_online_at)} <span style="color:var(--text-dim)">(${escapeHtml(w.last_pool_seen_text)})</span></td>
            <td class="act">
              <button class="link-btn" data-act="edit-worker-remote" data-name="${escapeHtml(w.worker_name)}">远控</button>
              <button class="link-btn" data-act="assign-worker" data-name="${escapeHtml(w.worker_name)}">分配</button>
              <button class="link-btn" data-act="edit-worker-share" data-name="${escapeHtml(w.worker_name)}" data-shares="${w.last_shares}">改份额</button>
              ${
                !w.is_currently_online
                  ? `<button class="link-btn danger" data-act="del-cached-worker" data-name="${escapeHtml(w.worker_name)}">移除</button>`
                  : ""
              }
            </td>
          </tr>
        `;
      })
      .join("");

    tbody.querySelectorAll("[data-act='edit-worker-remote']").forEach((b) => {
      b.onclick = () => {
        const w = (state.data?.workers || []).find((x) => x.worker_name === b.dataset.name);
        if (w) openWorkerRemoteModal(w);
      };
    });
    tbody.querySelectorAll("[data-act='assign-worker']").forEach((b) => {
      b.onclick = () => openQuickAssignModal(b.dataset.name);
    });
    tbody.querySelectorAll("[data-act='edit-worker-share']").forEach((b) => {
      b.onclick = () => openManualWorkerModal(b.dataset.name, Number(b.dataset.shares));
    });
    tbody.querySelectorAll("[data-act='del-cached-worker']").forEach((b) => {
      b.onclick = async () => {
        const ok = await confirmDialog(`确定从本地缓存移除离线矿机 "${b.dataset.name}" 吗？`, {
          title: "移除缓存矿机",
          confirmText: "确认移除",
          danger: true,
        });
        if (!ok) return;
        const next = await api().delete_cached_worker(state.data.active_wallet.id, b.dataset.name);
        applyStatePayload(next);
        toast("已移除缓存矿机");
      };
    });
  }

  function renderBlockRewards() {
    const ov = (state.data && state.data.active_wallet && state.data.active_wallet.overview) || {};
    const prlPrice = Number(ov.prl_price_cny || 0);
    const estDaily = Number(ov.est_daily_prl || 0);
    const reward24h = Number(ov.reward_24h_prl || 0);
    const rewards = Array.isArray(ov.block_rewards) ? ov.block_rewards : [];

    const dailyEl = document.getElementById("br-est-daily-pill");
    if (dailyEl) {
      const cnyTxt = prlPrice > 0 && estDaily > 0 ? ` (≈￥${(estDaily * prlPrice).toFixed(2)}/天)` : "";
      dailyEl.textContent = estDaily > 0 ? `预估日收益: ${estDaily.toFixed(4)} PRL${cnyTxt}` : "预估日收益: --";
    }
    const r24El = document.getElementById("br-24h-pill");
    if (r24El) {
      const cny24 = prlPrice > 0 && reward24h > 0 ? ` (≈￥${(reward24h * prlPrice).toFixed(2)})` : "";
      r24El.textContent = reward24h > 0 ? `24h 奖励: ${reward24h.toFixed(4)} PRL${cny24}` : "24h 奖励: --";
    }

    const totalEarned = rewards.reduce((acc, b) => acc + Number(b.earned_prl || 0), 0);
    const totalBadge = document.getElementById("br-total-badge");
    if (totalBadge) {
      const cnySum = prlPrice > 0 && totalEarned > 0 ? ` (≈￥${(totalEarned * prlPrice).toFixed(2)})` : "";
      totalBadge.textContent = `当前 ${rewards.length} 个区块合计: +${totalEarned.toFixed(6)} PRL${cnySum}`;
    }

    const toggleBtn = document.getElementById("btn-toggle-all-rewards");
    if (toggleBtn) {
      toggleBtn.textContent = state.showAllRewards
        ? `仅看最新 10 条 (共 ${rewards.length} 条)`
        : `展开全部 (共 ${rewards.length} 条)`;
      toggleBtn.style.display = rewards.length > 10 ? "" : "none";
    }

    const tbody = document.getElementById("block-rewards-tbody");
    if (!tbody) return;
    if (!rewards.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-state">暂无区块收益记录，请点击右上角「立即拉取」同步矿池页面</td></tr>`;
      return;
    }

    const visible = state.showAllRewards ? rewards : rewards.slice(0, 10);
    tbody.innerHTML = visible
      .map((b) => {
        const earned = Number(b.earned_prl || 0);
        const cnyStr = prlPrice > 0 ? `￥${(earned * prlPrice).toFixed(2)}` : "--";
        const isConfirmed = String(b.status || "").includes("确认") && !String(b.status || "").includes("待");
        const badgeCls = isConfirmed ? "status-online" : "status-offline";
        return `
          <tr>
            <td class="mono">${escapeHtml(b.found_at)}</td>
            <td class="num mono">${fmtInt(b.height)}</td>
            <td class="num mono c-muted">${escapeHtml(b.difficulty || "--")}</td>
            <td class="num mono">${Number(b.block_reward_prl || 0).toFixed(2)} PRL</td>
            <td class="num mono c-pri" style="font-weight:700">+${earned.toFixed(6)} PRL</td>
            <td class="num mono">${escapeHtml(cnyStr)}</td>
            <td><span class="status-badge ${badgeCls}">${escapeHtml(b.status || "已确认")}</span></td>
          </tr>
        `;
      })
      .join("");
  }

  // -------------------------------------------------------------------
  // Settlement View & Multi-Payout Merger
  // -------------------------------------------------------------------
  function buildMergedPaymentsPayload(selectedPayments) {
    if (!selectedPayments || !selectedPayments.length) return null;
    const sorted = [...selectedPayments].sort((a, b) =>
      String(b.paid_at || "").localeCompare(String(a.paid_at || ""))
    );
    const totalPrl = Number(
      sorted.reduce((acc, p) => acc + Number(p.amount_prl || 0), 0).toFixed(6)
    );
    const latestTime = sorted[0].paid_at || nowLocalStr();
    const earliestTime = sorted[sorted.length - 1].paid_at || latestTime;

    if (sorted.length === 1) {
      return {
        count: 1,
        time: latestTime,
        prl: totalPrl,
        label: "",
        tx_hash: `TX: ${sorted[0].tx_hash}`,
      };
    }

    const txSummary = sorted
      .map((p) => `${Number(p.amount_prl).toFixed(4)}PRL(${String(p.tx_hash || "").slice(0, 8)})`)
      .join(" + ");
    return {
      count: sorted.length,
      time: latestTime,
      prl: totalPrl,
      label: `合并 ${sorted.length} 笔矿池打款`,
      tx_hash: `合并 ${sorted.length} 笔打款 (${earliestTime.slice(0, 16)} ~ ${latestTime.slice(0, 16)}): ${txSummary}`,
    };
  }

  // -------------------------------------------------------------------
  // Persistent Settlement Drafts (Save & Resume for Offline Miners)
  // -------------------------------------------------------------------
  function collectCurrentDraftPayload(overrideTitle = null, overrideHint = null, forceNew = false) {
    if (!state.data?.active_wallet || !state.settleDraft) return null;
    return {
      wallet_id: state.data.active_wallet.id,
      draft_id: !forceNew && state.activeDraftId ? state.activeDraftId : null,
      title: overrideTitle !== null ? overrideTitle : state.activeDraftMeta?.title || "",
      offline_hint: overrideHint !== null ? overrideHint : state.activeDraftMeta?.offline_hint || "",
      prev_checkpoint_id: state.settlePrevCheckpointId ? Number(state.settlePrevCheckpointId) : null,
      checkpoint_time: document.getElementById("settle-time").value || nowLocalStr(),
      label: document.getElementById("settle-label").value || "",
      prl_amount: Number(document.getElementById("settle-prl").value || 0),
      cny_amount: Number(document.getElementById("settle-cny").value || 0),
      notes: document.getElementById("settle-notes").value || "",
      fees: collectFeeFormPayload(),
      worker_entries: state.settleDraft.workers || [],
    };
  }

  async function saveDraftDirect(forceNew = false, customTitle = null, customHint = null) {
    const payload = collectCurrentDraftPayload(customTitle, customHint, forceNew);
    if (!payload) {
      toast("当前结算草稿尚未初始化", "warn");
      return null;
    }
    const res = await api().save_settlement_draft(payload);
    state.activeDraftId = res.draft.id;
    state.activeDraftMeta = res.draft;
    applyStatePayload(res.state);
    toast(forceNew ? `已另存为新草稿：《${res.draft.title}》` : `已保存结算草稿：《${res.draft.title}》`);
    return res.draft;
  }

  function openSaveDraftModal(forceNew = false) {
    if (!state.settleDraft) return;
    const cny = Number(document.getElementById("settle-cny").value || 0);
    const cpTime = document.getElementById("settle-time").value || nowLocalStr();
    const labelVal = document.getElementById("settle-label").value.trim();
    const defaultTitle =
      (!forceNew && state.activeDraftMeta?.title) ||
      labelVal ||
      `${cpTime.slice(0, 16)} · ￥${cny.toFixed(2)} 结算草稿`;
    const offlineNames = (state.settleDraft.workers || [])
      .filter((w) => w.share_source !== "live")
      .map((w) => w.worker_name);
    const defaultHint =
      (!forceNew && state.activeDraftMeta?.offline_hint) ||
      (offlineNames.length
        ? `待确认离线矿机: ${offlineNames.slice(0, 4).join(", ")}${offlineNames.length > 4 ? ` 等${offlineNames.length}台` : ""}`
        : "");

    openModal(
      forceNew ? "另存为新提现草稿" : "保存提现结算草稿",
      `
        <div class="alert-box">
          保存草稿后会记录当前填写的到账人民币、PRL 数量以及<strong>每一台矿机当前定格份额</strong>。后续朋友出差回来开机上限（或核实历史份额）时，可一键恢复草稿继续修改。
        </div>
        <div class="form-field">
          <label>草稿名称 <span class="req">*</span></label>
          <input id="sd-title" class="input" value="${escapeHtml(defaultTitle)}" placeholder="例如：11月第3次提现（等小王出差回来）" />
        </div>
        <div class="form-field">
          <label>待补录离线矿机提示 (可选)</label>
          <input id="sd-hint" class="input" value="${escapeHtml(defaultHint)}" placeholder="例如：等待 bob_rig2 开机产生1份额后更新" />
        </div>
      `,
      [
        { text: "取消", onClick: closeModal },
        {
          text: forceNew ? "另存新草稿" : "保存草稿",
          cls: "btn-primary",
          onClick: async () => {
            const title = document.getElementById("sd-title").value.trim();
            const hint = document.getElementById("sd-hint").value.trim();
            if (!title) return toast("请输入草稿名称", "error");
            closeModal();
            await saveDraftDirect(forceNew, title, hint);
          },
        },
      ]
    );
  }

  async function resumeSettlementDraft(draftId) {
    try {
      const d = await api().load_settlement_draft(Number(draftId));
      state.activeDraftId = d.id;
      state.activeDraftMeta = d;
      state.settlePrevCheckpointId = d.prev_checkpoint_id ? Number(d.prev_checkpoint_id) : "";

      if (state.currentView !== "settle") {
        await switchView("settle");
      }

      // Ensure baseline dropdown has options loaded
      const cps = (state.data && state.data.checkpoints) || [];
      const selectEl = document.getElementById("settle-prev-checkpoint");
      const opts = [`<option value="">从 0 份额开始 (新钱包首次提现)</option>`].concat(
        cps.map((c) => {
          const tag = c.kind === "baseline" ? "基准起点" : `提现 ￥${fmtMoney(c.cny_amount)}`;
          const lbl = c.label ? ` · ${c.label}` : "";
          return `<option value="${c.id}">${escapeHtml(c.checkpoint_time)} (${escapeHtml(tag)}${escapeHtml(lbl)})</option>`;
        })
      );
      selectEl.innerHTML = opts.join("");
      selectEl.value = state.settlePrevCheckpointId ? String(state.settlePrevCheckpointId) : "";

      state.settleDraft = {
        now: d.checkpoint_time,
        workers: d.workers || [],
      };
      document.getElementById("settle-time").value = d.checkpoint_time || nowLocalStr();
      document.getElementById("settle-cny").value = d.cny_amount > 0 ? d.cny_amount : "";
      document.getElementById("settle-prl").value = d.prl_amount > 0 ? d.prl_amount : "";
      document.getElementById("settle-label").value = d.label || "";
      document.getElementById("settle-notes").value = d.notes || "";
      applyFeeFormPayload(d.fees || {});

      renderSettleDraftsPanel();
      await recomputeSettlePreview();
      toast(
        d.syncable_offline_count > 0
          ? `已恢复草稿《${d.title}》！检测到 ${d.syncable_offline_count} 台离线矿机已有新份额可同步`
          : `已恢复草稿《${d.title}》，请核实并修改未上线矿机份额`
      );
    } catch (e) {
      toast(`恢复草稿失败: ${e.message || e}`, "error");
    }
  }

  async function exitActiveDraft() {
    state.activeDraftId = null;
    state.activeDraftMeta = null;
    clearFeeFormPayload();
    renderSettleDraftsPanel();
    await initSettleDraft();
    toast("已退出草稿编辑并载入实时矿机数据");
  }

  function renderSettleDraftsPanel() {
    const panel = document.getElementById("settle-drafts-panel");
    if (!panel || !state.data) return;
    const drafts = state.data.drafts || [];
    const activeMeta = state.activeDraftId
      ? drafts.find((x) => x.id === state.activeDraftId) || state.activeDraftMeta
      : null;

    if (!drafts.length && !activeMeta) {
      panel.classList.add("hidden");
      panel.innerHTML = "";
      return;
    }

    panel.classList.remove("hidden");
    let html = "";

    if (activeMeta) {
      const liveWorkers = (state.data && state.data.workers) || [];
      const curNames = new Set((state.settleDraft?.workers || []).map((w) => w.worker_name));
      const newOnlineOrCached = liveWorkers.filter((lw) => !curNames.has(lw.worker_name));
      const importBtn = newOnlineOrCached.length
        ? `<button class="btn btn-sm btn-primary" id="btn-draft-import-new">🆕 导入 ${newOnlineOrCached.length} 台新出现矿机 (${escapeHtml(newOnlineOrCached.map((x) => x.worker_name).slice(0, 2).join(", "))})</button>`
        : "";

      html += `
        <div class="draft-active-banner">
          <div>
            <div style="font-weight:700;color:#38bdf8;">📌 正在编辑草稿：《${escapeHtml(activeMeta.title)}》</div>
            <div class="setting-desc">
              上次保存: ${escapeHtml(activeMeta.updated_at || "--")} · 在线矿机已按当时快照定格；你可修改任意离线矿机份额，或点击单行「📥 同步现值」仅同步刚上线的矿机
            </div>
          </div>
          <div class="panel-actions">
            ${importBtn}
            <button class="btn btn-sm btn-secondary" id="btn-draft-update-cur">💾 更新当前草稿</button>
            <button class="btn btn-sm btn-secondary" id="btn-draft-save-new">➕ 另存新草稿</button>
            <button class="btn btn-sm btn-secondary" id="btn-draft-exit">↩️ 退出草稿</button>
          </div>
        </div>
      `;
    }

    if (drafts.length) {
      html += `
        <div class="panel-header" style="margin-bottom:10px;">
          <div>
            <h2>📂 已保存的提现结算草稿 (${drafts.length})</h2>
            <p class="panel-desc">若还有朋友矿机未上线可先保存草稿，随时关闭软件或等其上线后一键恢复继续编辑</p>
          </div>
        </div>
        <div class="draft-card-list">
          ${drafts
            .map((d) => {
              const isCur = d.id === state.activeDraftId;
              const syncPill =
                d.syncable_offline_count > 0
                  ? `<span class="status-badge status-online">💡 ${d.syncable_offline_count} 台离线矿机已有新份额</span>`
                  : "";
              const offPill =
                d.offline_count > 0
                  ? `<span class="status-badge status-offline">⏳ 非在线 ${d.offline_count} 台</span>`
                  : `<span class="status-badge status-manual">全部就绪</span>`;
              return `
                <div class="draft-card ${isCur ? "active" : ""}">
                  <div>
                    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                      <strong>${escapeHtml(d.title)}</strong>
                      ${offPill}
                      ${syncPill}
                      ${isCur ? `<span class="status-badge status-online">当前编辑中</span>` : ""}
                    </div>
                    <div class="setting-desc" style="margin-top:4px;">
                      结算时间: ${escapeHtml(d.checkpoint_time)} · 实收 <strong class="c-pri">￥${fmtMoney(d.cny_amount)}</strong> / ${fmtPrl(d.prl_amount)} PRL · 总增量 +${fmtInt(d.total_delta_shares)} · 更新于 ${escapeHtml(d.updated_at)}
                      ${d.offline_hint ? ` · <span class="c-gold">${escapeHtml(d.offline_hint)}</span>` : ""}
                    </div>
                  </div>
                  <div class="panel-actions">
                    <button class="btn btn-sm ${isCur ? "btn-secondary" : "btn-primary"}" data-draft-resume="${d.id}">${isCur ? "重新载入草稿" : "📂 恢复继续编辑"}</button>
                    <button class="btn btn-sm btn-danger" data-draft-del="${d.id}">删除</button>
                  </div>
                </div>
              `;
            })
            .join("")}
        </div>
      `;
    }

    panel.innerHTML = html;

    document.getElementById("btn-draft-update-cur")?.addEventListener("click", () => saveDraftDirect(false));
    document.getElementById("btn-draft-save-new")?.addEventListener("click", () => openSaveDraftModal(true));
    document.getElementById("btn-draft-exit")?.addEventListener("click", exitActiveDraft);
    document.getElementById("btn-draft-import-new")?.addEventListener("click", async () => {
      if (!state.settleDraft) return;
      const members = state.data.members || [];
      const mById = new Map(members.map((m) => [m.id, m]));
      const curNames = new Set((state.settleDraft.workers || []).map((w) => w.worker_name));
      const newRows = (state.data.workers || []).filter((lw) => !curNames.has(lw.worker_name));
      newRows.forEach((lw) => {
        const mObj = lw.member_id ? mById.get(lw.member_id) : null;
        state.settleDraft.workers.push({
          worker_name: lw.worker_name,
          member_id: lw.member_id,
          member_name: mObj ? mObj.name : lw.member_name || "未分配",
          member_color: mObj ? mObj.color : lw.member_color || "#64748b",
          prev_shares: 0,
          curr_shares: Number(lw.last_shares || 0),
          share_source: lw.is_currently_online ? "live" : lw.source || "offline_cache",
          is_online: Boolean(lw.is_currently_online),
          last_seen_at: lw.last_seen_online_at || "",
          hashrate: lw.current_hashrate || "--",
        });
      });
      await recomputeSettlePreview();
      renderSettleDraftsPanel();
      toast(`已将 ${newRows.length} 台新增矿机导入当前草稿`);
    });

    panel.querySelectorAll("[data-draft-resume]").forEach((btn) => {
      btn.onclick = () => resumeSettlementDraft(Number(btn.dataset.draftResume));
    });
    panel.querySelectorAll("[data-draft-del]").forEach((btn) => {
      btn.onclick = async () => {
        const id = Number(btn.dataset.draftDel);
        const ok = await confirmDialog("确定永久删除这条结算草稿吗？删除后不会影响正式账本记录。", {
          title: "删除结算草稿",
          confirmText: "确认删除",
          danger: true,
        });
        if (!ok) return;
        const wid = state.data?.active_wallet?.id || null;
        const next = await api().delete_settlement_draft(id, wid);
        if (state.activeDraftId === id) {
          state.activeDraftId = null;
          state.activeDraftMeta = null;
        }
        applyStatePayload(next);
        toast("草稿已删除");
      };
    });
  }

  async function initSettleDraft(prefill = null) {
    if (!state.data || !state.data.active_wallet) return;
    if (prefill) {
      state.activeDraftId = null;
      state.activeDraftMeta = null;
    }
    const wid = state.data.active_wallet.id;
    const cps = state.data.checkpoints || [];

    const selectEl = document.getElementById("settle-prev-checkpoint");
    const opts = [`<option value="">从 0 份额开始 (新钱包首次提现)</option>`].concat(
      cps.map((c) => {
        const tag = c.kind === "baseline" ? "基准起点" : `提现 ￥${fmtMoney(c.cny_amount)}`;
        const lbl = c.label ? ` · ${c.label}` : "";
        return `<option value="${c.id}">${escapeHtml(c.checkpoint_time)} (${escapeHtml(tag)}${escapeHtml(lbl)})</option>`;
      })
    );
    selectEl.innerHTML = opts.join("");

    if (state.settlePrevCheckpointId === null) {
      state.settlePrevCheckpointId = cps.length > 0 ? cps[0].id : "";
    }
    selectEl.value = state.settlePrevCheckpointId ? String(state.settlePrevCheckpointId) : "";

    const draft = await api().preview_settlement(
      wid,
      state.settlePrevCheckpointId ? Number(state.settlePrevCheckpointId) : null
    );
    state.settleDraft = draft;

    document.getElementById("settle-time").value = (prefill && prefill.time) || draft.now || nowLocalStr();
    if (prefill && prefill.prl != null) {
      document.getElementById("settle-prl").value = prefill.prl;
    }
    if (prefill && prefill.cny != null) {
      document.getElementById("settle-cny").value = prefill.cny;
    }
    if (prefill && prefill.label) {
      const lblInput = document.getElementById("settle-label");
      if (lblInput && !lblInput.value.trim()) {
        lblInput.value = prefill.label;
      }
    }
    if (prefill && prefill.tx_hash) {
      document.getElementById("settle-notes").value = prefill.tx_hash;
    }
    if (prefill && prefill.fees) {
      applyFeeFormPayload(prefill.fees);
      setFeesPanelExpanded(true);
    } else if (!state.activeDraftId && !prefill) {
      syncFeeWalletTypeLock();
      updateFeeLiveSummary();
    }
    await recomputeSettlePreview();
  }

  async function recomputeSettlePreview() {
    if (!state.settleDraft) return;
    const cny = Number(document.getElementById("settle-cny").value || 0);
    const prl = Number(document.getElementById("settle-prl").value || 0);

    const res = await api().calculate_preview_split(state.settleDraft.workers, cny, prl);
    state.settleSplit = res;

    document.getElementById("settle-preview-summary").textContent =
      `区间总增量份额: ${fmtInt(res.total_delta_shares)} | 实收: ￥${fmtMoney(cny)}` +
      (res.total_delta_shares > 0 && cny > 0
        ? ` | 约合 ￥${((cny / res.total_delta_shares) * 10000).toFixed(2)} / 万份额`
        : "");
    const rpHintEl = document.getElementById("settle-round-paid-hint");
    const rp = state.data && state.data.round_pending_info;
    if (rpHintEl && rp) {
      const paidSince = Number(rp.paid_since_baseline_prl || 0);
      if (paidSince > 0) {
        rpHintEl.innerHTML =
          `参考：自上次基准以来矿池已累计支付 <strong style="color:var(--primary)">${paidSince.toFixed(6)} PRL</strong>` +
          `（另有 ${Number(rp.pool_pending_prl || 0).toFixed(4)} PRL 待发）` +
          ` <button type="button" class="link-btn" id="btn-fill-round-paid-prl" style="padding:0;margin-left:4px;font-size:11px;">填入已付</button>`;
        const fillBtn = document.getElementById("btn-fill-round-paid-prl");
        if (fillBtn) {
          fillBtn.onclick = () => {
            document.getElementById("settle-prl").value = String(Number(paidSince.toFixed(6)));
            recomputeSettlePreview();
          };
        }
      } else {
        rpHintEl.textContent = "";
      }
    }
    updateFeeLiveSummary();

    // Ratio Bar & Legend
    const bar = document.getElementById("settle-ratio-bar");
    const legend = document.getElementById("settle-ratio-legend");
    const nonZero = res.allocations.filter((a) => a.delta_shares > 0);
    bar.innerHTML = nonZero.length
      ? nonZero
          .map(
            (a) =>
              `<div class="ratio-seg" style="width:${(a.ratio * 100).toFixed(2)}%;background:${escapeHtml(a.member_color)}" title="${escapeHtml(a.member_name)} ${(a.ratio * 100).toFixed(2)}%"></div>`
          )
          .join("")
      : `<div style="width:100%;background:transparent"></div>`;

    if (legend) {
      legend.innerHTML = nonZero
        .map(
          (a) => `
          <span class="ratio-legend-item">
            <span class="member-dot" style="background:${escapeHtml(a.member_color)}"></span>
            <strong>${escapeHtml(a.member_name)}</strong>
            <span>${(a.ratio * 100).toFixed(2)}%</span>
            <span style="color:#34d399">￥${fmtMoney(a.cny_earned)}</span>
          </span>
        `
        )
        .join("");
    }

    // Member Allocations Table
    const allocBody = document.getElementById("settle-alloc-tbody");
    if (!res.allocations.length) {
      allocBody.innerHTML = `<tr><td colspan="6" class="empty-state">暂无矿机增量份额数据</td></tr>`;
    } else {
      allocBody.innerHTML = res.allocations
        .map(
          (a) => `
          <tr>
            <td>
              <span class="member-tag">
                <span class="member-dot" style="background:${escapeHtml(a.member_color)}"></span>
                ${escapeHtml(a.member_name)}
              </span>
            </td>
            <td class="num" style="color:var(--text-muted)">${a.active_worker_count ?? a.worker_count ?? 0}/${a.worker_count ?? 0} 台</td>
            <td class="num">+${fmtInt(a.delta_shares)}</td>
            <td class="num">${(a.ratio * 100).toFixed(2)}%</td>
            <td class="num">${Number(a.prl_earned || 0).toFixed(6)} PRL</td>
            <td class="num"><strong style="color:#34d399;font-size:15px;">￥${fmtMoney(a.cny_earned)}</strong></td>
          </tr>
        `
        )
        .join("");
    }

    // Worker rows table (respecting settleWorkerFilter while keeping original idx)
    const wBody = document.getElementById("settle-workers-tbody");
    const members = state.data.members || [];
    const indexedWorkers = res.workers
      .map((w, idx) => ({ w, idx }))
      .filter(({ w }) => {
        if (state.settleWorkerFilter === "delta") return w.delta_shares > 0;
        if (state.settleWorkerFilter === "offline") {
          return w.share_source !== "live" || w.reset_detected || w.member_id === null;
        }
        return true;
      });

    if (!indexedWorkers.length) {
      wBody.innerHTML = `<tr><td colspan="7" class="empty-state">当前筛选条件下无矿机记录</td></tr>`;
      return;
    }

    const liveCacheMap = new Map(
      ((state.data && state.data.workers) || []).map((cw) => [cw.worker_name, cw])
    );

    wBody.innerHTML = indexedWorkers
      .map(({ w, idx }) => {
        const srcBadge =
          w.share_source === "live"
            ? `<span class="status-badge status-online">在线实时</span>`
            : w.share_source === "offline_cache"
            ? `<span class="status-badge status-offline">离线缓存</span>`
            : w.share_source === "manual"
            ? `<span class="status-badge status-manual">手动录入</span>`
            : `<span class="status-badge status-missing">离线未更新</span>`;

        const memberSelect = `
          <select class="input-sm settle-worker-member" data-idx="${idx}" style="width:125px;">
            <option value="">未分配</option>
            ${members
              .map(
                (m) =>
                  `<option value="${m.id}" ${w.member_id === m.id ? "selected" : ""}>${escapeHtml(m.name)}</option>`
              )
              .join("")}
          </select>
        `;

        const resetBtn = w.reset_detected
          ? `<button class="link-btn" data-idx="${idx}" data-act="toggle-reset" title="当前份额小于上次记录，点击切换是否按重置计算">${w.treat_reset_as_curr ? "已按重置" : "⚠️疑似重置"}</button>`
          : "";

        const liveCw = liveCacheMap.get(w.worker_name);
        const liveDiff = liveCw && Number(liveCw.last_shares) !== Number(w.curr_shares);
        const syncLiveBtn = liveDiff
          ? `<button class="link-btn" data-idx="${idx}" data-act="sync-worker-cache" title="仅将该矿机的「本次份额」更新为当前最新缓存值 (${fmtInt(liveCw.last_shares)})，不影响其他已定格矿机">📥 同步现值(${fmtInt(liveCw.last_shares)})</button>`
          : "";

        return `
          <tr>
            <td><strong>${escapeHtml(w.worker_name)}</strong></td>
            <td>${memberSelect}</td>
            <td>${srcBadge}</td>
            <td class="num">
              <input type="number" class="input inline-share-input settle-prev-input" data-idx="${idx}" min="0" value="${w.prev_shares}" />
            </td>
            <td class="num">
              <input type="number" class="input inline-share-input settle-curr-input" data-idx="${idx}" min="0" value="${w.curr_shares}" />
            </td>
            <td class="num">
              <strong style="color:${w.delta_shares > 0 ? "#34d399" : "var(--text-dim)"}">+${fmtInt(w.delta_shares)}</strong>
              ${resetBtn}
            </td>
            <td class="act">
              ${syncLiveBtn}
              ${
                w.member_id === null
                  ? `<button class="link-btn" data-act="settle-quick-assign" data-name="${escapeHtml(w.worker_name)}">建前缀</button>`
                  : ""
              }
            </td>
          </tr>
        `;
      })
      .join("");

    wBody.querySelectorAll(".settle-prev-input").forEach((inp) => {
      inp.onchange = async () => {
        const i = Number(inp.dataset.idx);
        state.settleDraft.workers[i].prev_shares = Math.max(0, parseInt(inp.value || "0", 10));
        await recomputeSettlePreview();
      };
    });

    wBody.querySelectorAll(".settle-curr-input").forEach((inp) => {
      inp.onchange = async () => {
        const i = Number(inp.dataset.idx);
        state.settleDraft.workers[i].curr_shares = Math.max(0, parseInt(inp.value || "0", 10));
        state.settleDraft.workers[i].share_source = "manual";
        await recomputeSettlePreview();
      };
    });

    wBody.querySelectorAll("[data-act='sync-worker-cache']").forEach((btn) => {
      btn.onclick = async () => {
        const i = Number(btn.dataset.idx);
        const w = state.settleDraft.workers[i];
        const liveCw = liveCacheMap.get(w.worker_name);
        if (!liveCw) return;
        w.curr_shares = Number(liveCw.last_shares || 0);
        w.share_source = liveCw.is_currently_online ? "live" : liveCw.source || "offline_cache";
        await recomputeSettlePreview();
        toast(`已将 ${w.worker_name} 同步为最新份额 ${fmtInt(w.curr_shares)}`);
      };
    });

    wBody.querySelectorAll(".settle-worker-member").forEach((sel) => {
      sel.onchange = async () => {
        const i = Number(sel.dataset.idx);
        const mid = sel.value ? Number(sel.value) : null;
        state.settleDraft.workers[i].member_id = mid;
        const mObj = members.find((m) => m.id === mid);
        state.settleDraft.workers[i].member_name = mObj ? mObj.name : "未分配";
        state.settleDraft.workers[i].member_color = mObj ? mObj.color : "#64748b";
        await recomputeSettlePreview();
      };
    });

    wBody.querySelectorAll("[data-act='toggle-reset']").forEach((btn) => {
      btn.onclick = async () => {
        const i = Number(btn.dataset.idx);
        state.settleDraft.workers[i].treat_reset_as_curr = !state.settleDraft.workers[i].treat_reset_as_curr;
        await recomputeSettlePreview();
      };
    });

    wBody.querySelectorAll("[data-act='settle-quick-assign']").forEach((btn) => {
      btn.onclick = () => openQuickAssignModal(btn.dataset.name, async () => {
        await initSettleDraft();
      });
    });
  }

  // -------------------------------------------------------------------
  // Custom Fee Pipeline, Conversions & Multi-Asset Residual Helpers
  // -------------------------------------------------------------------
  const FEE_CATEGORY_OPTS = [
    ["transfer", "钱包转账"],
    ["trade", "交易所卖币"],
    ["withdraw", "平台提现"],
    ["fx", "跨境换汇"],
    ["payout", "银行/收款出金"],
    ["other", "其他"],
  ];

  function formatSignedDust(v, unit = "") {
    const n = Number(v || 0);
    if (Math.abs(n) < 1e-8) return `0${unit ? " " + unit : ""}`;
    const sign = n > 0 ? "+" : "";
    return `${sign}${Number(n.toFixed(6))}${unit ? " " + unit : ""}`;
  }

  function formatDustPoolBadgeText(rs) {
    const bals = (rs && rs.balances) || {};
    const keys = Object.keys(bals).filter((k) => k === "PRL" || k === "USDT" || Math.abs(Number(bals[k] || 0)) > 1e-8);
    if (!keys.length) return "残余池 PRL: 0 · USDT: 0";
    return "残余池 " + keys.map((k) => `${k}: ${fmtPrl(bals[k] || 0)}`).join(" · ");
  }

  function makeDefaultFeeFormState() {
    const templates = (state.data && state.data.fee_pipeline_templates) || [];
    const rsBals = (state.data && state.data.residual_summary && state.data.residual_summary.balances) || {};
    const feeEntries = templates
      .filter((t) => t.enabled !== false)
      .map((t) => ({
        step_id: t.id,
        label: t.name,
        category: t.category || "other",
        amount: Number(t.default_fee || 0),
        currency: t.fee_currency || "USDT",
        only_official_wallet: Boolean(t.only_official_wallet),
      }));
    const resMap = { PRL: 0, USDT: 0 };
    Object.keys(rsBals).forEach((k) => {
      if (!(k in resMap) && Math.abs(Number(rsBals[k] || 0)) > 1e-8) resMap[k] = 0;
    });
    return {
      fee_entries: feeEntries,
      conversions: [],
      fx_rates_cny: [],
      residual_deltas: resMap,
    };
  }

  function ensureFeeFormState() {
    if (!state.feeFormState) {
      state.feeFormState = makeDefaultFeeFormState();
    }
    return state.feeFormState;
  }

  function setFeesPanelExpanded(open) {
    const body = document.getElementById("settle-fees-body");
    const btn = document.getElementById("btn-toggle-fees-panel");
    if (!body || !btn) return;
    body.classList.toggle("hidden", !open);
    btn.textContent = open ? "收起明细 ▴" : "展开填写 ▾";
  }

  function syncFeeWalletTypeLock() {
    const aw = state.data && state.data.active_wallet;
    const isEx = Boolean(aw && aw.wallet_type === "exchange");
    const note = document.getElementById("fee-wallet-type-note");
    if (note) {
      note.innerHTML = isEx
        ? `当前钱包类别：<strong style="color:#34d399">交易所钱包</strong> — 矿池直接转入交易所，所有勾选「仅官方钱包」的转账手续费项已自动免除/锁定（可在顶部点「编辑」切换钱包类别）。`
        : `当前钱包类别：<strong style="color:#38bdf8">Pearl 官方钱包</strong> — 可记录官方钱包转出到交易所的上链转账费；右侧或顶部可点「⚙️ 自定义手续费流程」增删自己的环节。`;
    }
    const rs = (state.data && state.data.residual_summary) || {};
    const poolBadge = document.getElementById("settle-dust-pool-badge");
    if (poolBadge) {
      poolBadge.textContent = formatDustPoolBadgeText(rs);
    }
    renderFeeDynamicPanels();
  }

  function renderFeeDynamicPanels() {
    const fs = ensureFeeFormState();
    const aw = state.data && state.data.active_wallet;
    const isEx = Boolean(aw && aw.wallet_type === "exchange");
    const rs = (state.data && state.data.residual_summary) || {};
    const rsBals = rs.balances || { PRL: 0, USDT: 0 };

    // 1. Dynamic Fee Steps
    const stepsEl = document.getElementById("fee-dynamic-steps-list");
    if (stepsEl) {
      if (!fs.fee_entries.length) {
        stepsEl.innerHTML = `<div class="empty-state" style="padding:12px;">当前流程无启用手续费步骤，点右上角「+ 临时加一项」或「⚙️ 编辑流程模板」添加</div>`;
      } else {
        stepsEl.innerHTML = fs.fee_entries
          .map((item, idx) => {
            const locked = Boolean(item.only_official_wallet && isEx);
            const valStr = !locked && Number(item.amount || 0) > 0 ? String(item.amount) : "";
            const catLabel = (FEE_CATEGORY_OPTS.find((x) => x[0] === item.category) || [, "其他"])[1];
            return `
              <div class="fee-step-row ${locked ? "locked" : ""}">
                <div class="fee-step-meta">
                  <div class="fee-step-name">
                    <span>${escapeHtml(item.label)}</span>
                    <span class="wallet-type-tag official" style="font-size:10px;padding:1px 6px;">${escapeHtml(catLabel)}</span>
                    ${item.only_official_wallet ? `<span class="status-badge status-manual" style="font-size:10px;">仅官方钱包</span>` : ""}
                  </div>
                  <div class="fee-step-sub">${locked ? "当前为交易所直充钱包，本项已自动免除" : `手续费币种可随时改成任意币种（如 PRL / USDT / USD / EUR / CNY）`}</div>
                </div>
                <div class="fee-step-inputs">
                  <input type="number" class="input-sm fee-step-amt" data-idx="${idx}" step="0.000001" min="0" value="${escapeHtml(valStr)}" ${locked ? "disabled" : ""} placeholder="${locked ? "免收" : "0"}" style="width:105px;" />
                  <input type="text" class="input-sm fee-step-cur mono-addr" data-idx="${idx}" value="${escapeHtml(item.currency || "USDT")}" ${locked ? "disabled" : ""} placeholder="币种" style="width:68px;text-transform:uppercase;" />
                  <button type="button" class="link-btn danger fee-step-del" data-idx="${idx}" title="从本轮移除此项">&times;</button>
                </div>
              </div>
            `;
          })
          .join("");

        stepsEl.querySelectorAll(".fee-step-amt").forEach((inp) => {
          inp.oninput = () => {
            const i = Number(inp.dataset.idx);
            fs.fee_entries[i].amount = Math.max(0, Number(inp.value || 0));
            updateFeeLiveSummary();
          };
        });
        stepsEl.querySelectorAll(".fee-step-cur").forEach((inp) => {
          inp.oninput = () => {
            const i = Number(inp.dataset.idx);
            fs.fee_entries[i].currency = inp.value.trim().toUpperCase().slice(0, 10) || "USDT";
            updateFeeLiveSummary();
          };
        });
        stepsEl.querySelectorAll(".fee-step-del").forEach((btn) => {
          btn.onclick = () => {
            fs.fee_entries.splice(Number(btn.dataset.idx), 1);
            renderFeeDynamicPanels();
            updateFeeLiveSummary();
          };
        });
      }
    }

    // 2. Dynamic Conversion Legs
    const convsEl = document.getElementById("fee-conversions-list");
    if (convsEl) {
      if (!fs.conversions.length) {
        convsEl.innerHTML = `<div class="empty-state" style="padding:10px;font-size:12px;">如需记录卖币单价或汇损，点右上角「+ 添加一段兑换」（如 PRL→USDT、USDT→法币、换汇购汇等）</div>`;
      } else {
        convsEl.innerHTML = fs.conversions
          .map(
            (cv, idx) => `
            <div class="fee-conv-card">
              <div class="fee-conv-row">
                <input type="text" class="input-sm conv-label" data-idx="${idx}" value="${escapeHtml(cv.label || "")}" placeholder="阶段说明（如 交易所卖币）" />
                <input type="text" class="input-sm conv-from mono-addr" data-idx="${idx}" value="${escapeHtml(cv.from_asset || "PRL")}" placeholder="卖出币种" style="text-transform:uppercase;" />
                <input type="text" class="input-sm conv-to mono-addr" data-idx="${idx}" value="${escapeHtml(cv.to_asset || "USDT")}" placeholder="买入币种" style="text-transform:uppercase;" />
                <button type="button" class="link-btn danger conv-del" data-idx="${idx}" title="删除本段兑换">&times;</button>
              </div>
              <div class="fee-conv-nums">
                <input type="number" class="input-sm conv-sold" data-idx="${idx}" step="0.000001" min="0" value="${cv.sold_amount > 0 ? cv.sold_amount : ""}" placeholder="卖出数量" title="实际卖出源币数量" />
                <input type="number" class="input-sm conv-price" data-idx="${idx}" step="0.000001" min="0" value="${cv.unit_price > 0 ? cv.unit_price : ""}" placeholder="成交单价" title="1 卖出币种 = ? 买入币种" />
                <input type="number" class="input-sm conv-rec" data-idx="${idx}" step="0.000001" min="0" value="${cv.received_amount > 0 ? cv.received_amount : ""}" placeholder="实得数量" title="自动计算或手动修改实得目标币" />
                <input type="number" class="input-sm conv-mkt" data-idx="${idx}" step="0.000001" min="0" value="${cv.market_ref_rate > 0 ? cv.market_ref_rate : ""}" placeholder="中间参考价(算汇损)" title="可选：若填参考汇率将自动算汇差损耗" />
              </div>
            </div>
          `
          )
          .join("");

        convsEl.querySelectorAll(".conv-label").forEach((el) => {
          el.oninput = () => {
            fs.conversions[Number(el.dataset.idx)].label = el.value;
            updateFeeLiveSummary();
          };
        });
        convsEl.querySelectorAll(".conv-from").forEach((el) => {
          el.oninput = () => {
            fs.conversions[Number(el.dataset.idx)].from_asset = el.value.trim().toUpperCase().slice(0, 10) || "PRL";
            updateFeeLiveSummary();
          };
        });
        convsEl.querySelectorAll(".conv-to").forEach((el) => {
          el.oninput = () => {
            fs.conversions[Number(el.dataset.idx)].to_asset = el.value.trim().toUpperCase().slice(0, 10) || "USDT";
            updateFeeLiveSummary();
          };
        });
        convsEl.querySelectorAll(".conv-sold").forEach((el) => {
          el.oninput = () => {
            const i = Number(el.dataset.idx);
            fs.conversions[i].sold_amount = Math.max(0, Number(el.value || 0));
            const recInp = convsEl.querySelector(`.conv-rec[data-idx="${i}"]`);
            if (fs.conversions[i].sold_amount > 0 && fs.conversions[i].unit_price > 0 && recInp && !recInp.dataset.manual) {
              const autoRec = Number((fs.conversions[i].sold_amount * fs.conversions[i].unit_price).toFixed(6));
              fs.conversions[i].received_amount = autoRec;
              recInp.value = String(autoRec);
            }
            updateFeeLiveSummary();
          };
        });
        convsEl.querySelectorAll(".conv-price").forEach((el) => {
          el.oninput = () => {
            const i = Number(el.dataset.idx);
            fs.conversions[i].unit_price = Math.max(0, Number(el.value || 0));
            const recInp = convsEl.querySelector(`.conv-rec[data-idx="${i}"]`);
            if (fs.conversions[i].sold_amount > 0 && fs.conversions[i].unit_price > 0 && recInp && !recInp.dataset.manual) {
              const autoRec = Number((fs.conversions[i].sold_amount * fs.conversions[i].unit_price).toFixed(6));
              fs.conversions[i].received_amount = autoRec;
              recInp.value = String(autoRec);
            }
            updateFeeLiveSummary();
          };
        });
        convsEl.querySelectorAll(".conv-rec").forEach((el) => {
          el.oninput = () => {
            const i = Number(el.dataset.idx);
            el.dataset.manual = el.value.trim() ? "1" : "";
            fs.conversions[i].received_amount = Math.max(0, Number(el.value || 0));
            updateFeeLiveSummary();
          };
        });
        convsEl.querySelectorAll(".conv-mkt").forEach((el) => {
          el.oninput = () => {
            fs.conversions[Number(el.dataset.idx)].market_ref_rate = Math.max(0, Number(el.value || 0));
            updateFeeLiveSummary();
          };
        });
        convsEl.querySelectorAll(".conv-del").forEach((btn) => {
          btn.onclick = () => {
            fs.conversions.splice(Number(btn.dataset.idx), 1);
            renderFeeDynamicPanels();
            updateFeeLiveSummary();
          };
        });
      }
    }

    // 3. FX Reference Rates
    const fxEl = document.getElementById("fee-fx-rates-list");
    if (fxEl) {
      fxEl.innerHTML = fs.fx_rates_cny
        .map(
          (row, idx) => `
          <div class="fee-fx-chip">
            <input type="text" class="input-sm fx-asset mono-addr" data-idx="${idx}" value="${escapeHtml(row.asset)}" placeholder="USDT" style="width:62px;text-transform:uppercase;" />
            <span style="font-size:11.5px;color:var(--text-dim);">/CNY =</span>
            <input type="number" class="input-sm fx-val" data-idx="${idx}" step="0.0001" min="0" value="${row.rate > 0 ? row.rate : ""}" placeholder="如 7.25" style="width:80px;" />
            <button type="button" class="link-btn danger fx-del" data-idx="${idx}" style="padding:0 3px;">&times;</button>
          </div>
        `
        )
        .join("");

      fxEl.querySelectorAll(".fx-asset").forEach((el) => {
        el.oninput = () => {
          fs.fx_rates_cny[Number(el.dataset.idx)].asset = el.value.trim().toUpperCase().slice(0, 10);
          updateFeeLiveSummary();
        };
      });
      fxEl.querySelectorAll(".fx-val").forEach((el) => {
        el.oninput = () => {
          fs.fx_rates_cny[Number(el.dataset.idx)].rate = Math.max(0, Number(el.value || 0));
          updateFeeLiveSummary();
        };
      });
      fxEl.querySelectorAll(".fx-del").forEach((btn) => {
        btn.onclick = () => {
          fs.fx_rates_cny.splice(Number(btn.dataset.idx), 1);
          renderFeeDynamicPanels();
          updateFeeLiveSummary();
        };
      });
    }

    // 4. Multi-Asset Residuals
    const resEl = document.getElementById("fee-residuals-list");
    if (resEl) {
      const assets = Array.from(new Set(["PRL", "USDT", ...Object.keys(fs.residual_deltas || {}), ...Object.keys(rsBals)]));
      resEl.innerHTML = assets
        .map((asset) => {
          const curBal = Number(rsBals[asset] || 0);
          const deltaVal = Number((fs.residual_deltas && fs.residual_deltas[asset]) || 0);
          const nextBal = state.residualSource === "trades" ? curBal : Number((curBal + deltaVal).toFixed(6));
          const deltaCls = deltaVal > 0 ? "dust-delta-pos" : deltaVal < 0 ? "dust-delta-neg" : "";
          const hintTxt =
            state.residualSource === "trades"
              ? `已含自导入流水 (${formatSignedDust(deltaVal, asset)}) · 现余 ${fmtPrl(curBal)}`
              : `残余池: ${fmtPrl(curBal)} → ${fmtPrl(nextBal)} ${asset}`;
          return `
            <div class="fee-residual-card">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px;">
                <strong style="font-size:12.5px;">${escapeHtml(asset)} 精度残余变动</strong>
                <button type="button" class="link-btn js-calibrate-dust-btn" style="padding:0;font-size:11px;">设置现值</button>
              </div>
              <input type="number" class="input-sm fee-res-input" data-asset="${escapeHtml(asset)}" step="0.000001" value="${Math.abs(deltaVal) > 1e-9 ? deltaVal : ""}" placeholder="正数留余 / 负数消耗旧余" style="width:100%;" />
              <div class="fee-dust-preview ${deltaCls}" id="fee-res-prev-${escapeHtml(asset)}">${escapeHtml(hintTxt)}</div>
            </div>
          `;
        })
        .join("");

      resEl.querySelectorAll(".fee-res-input").forEach((inp) => {
        inp.oninput = () => {
          const asset = inp.dataset.asset;
          const v = Number(inp.value || 0);
          fs.residual_deltas[asset] = Number.isFinite(v) ? v : 0;
          state.residualSource = state.linkedTradeIds.length ? "trades" : "direct";
          const curBal = Number(rsBals[asset] || 0);
          const nextBal = state.residualSource === "trades" ? curBal : Number((curBal + (v || 0)).toFixed(6));
          const prevEl = document.getElementById(`fee-res-prev-${asset}`);
          if (prevEl) {
            prevEl.className = `fee-dust-preview ${v > 0 ? "dust-delta-pos" : v < 0 ? "dust-delta-neg" : ""}`;
            prevEl.textContent =
              state.residualSource === "trades"
                ? `已含自导入流水 (${formatSignedDust(v, asset)}) · 现余 ${fmtPrl(curBal)}`
                : `残余池: ${fmtPrl(curBal)} → ${fmtPrl(nextBal)} ${asset}`;
          }
          updateFeeLiveSummary();
        };
      });
    }
  }

  function collectFeeFormPayload() {
    const aw = state.data && state.data.active_wallet;
    const wtype = aw && aw.wallet_type === "exchange" ? "exchange" : "official";
    const fs = ensureFeeFormState();
    const feeEntries = [];
    fs.fee_entries.forEach((it) => {
      if (!it.label) return;
      const amt = it.only_official_wallet && wtype === "exchange" ? 0 : Math.max(0, Number(it.amount || 0));
      if (amt > 0) {
        feeEntries.push({
          step_id: it.step_id,
          label: it.label,
          category: it.category || "other",
          amount: Number(amt.toFixed(6)),
          currency: (it.currency || "USDT").trim().toUpperCase(),
          only_official_wallet: Boolean(it.only_official_wallet),
        });
      }
    });
    const conversions = (fs.conversions || [])
      .filter((cv) => (cv.sold_amount || 0) > 0 || (cv.unit_price || 0) > 0 || (cv.received_amount || 0) > 0)
      .map((cv) => ({
        label: cv.label || `${cv.from_asset || "PRL"} → ${cv.to_asset || "USDT"}`,
        from_asset: (cv.from_asset || "PRL").trim().toUpperCase(),
        to_asset: (cv.to_asset || "USDT").trim().toUpperCase(),
        sold_amount: Math.max(0, Number(cv.sold_amount || 0)),
        unit_price: Math.max(0, Number(cv.unit_price || 0)),
        received_amount: Math.max(0, Number(cv.received_amount || 0)),
        market_ref_rate: Math.max(0, Number(cv.market_ref_rate || 0)),
      }));
    const fxDict = {};
    (fs.fx_rates_cny || []).forEach((r) => {
      const code = (r.asset || "").trim().toUpperCase();
      const val = Number(r.rate || 0);
      if (code && code !== "CNY" && val > 0) fxDict[code] = val;
    });
    const resDict = {};
    Object.entries(fs.residual_deltas || {}).forEach(([k, v]) => {
      const num = Number(v || 0);
      if (k && Math.abs(num) > 1e-8) resDict[k] = Number(num.toFixed(6));
    });
    return {
      schema_version: 2,
      wallet_type: wtype,
      fee_entries: feeEntries,
      conversions,
      fx_rates_cny: fxDict,
      residual_deltas: resDict,
      residual_source: state.residualSource || "direct",
      linked_trade_ids: Array.isArray(state.linkedTradeIds) ? state.linkedTradeIds : [],
    };
  }

  function applyFeeFormPayload(fees = {}) {
    const f = fees || {};
    const base = makeDefaultFeeFormState();
    const byId = new Map();
    base.fee_entries.forEach((e) => byId.set(e.step_id, { ...e }));
    const extra = [];
    (f.fee_entries || []).forEach((it) => {
      const sid = String(it.step_id || "");
      if (sid && byId.has(sid)) {
        const target = byId.get(sid);
        target.amount = Number(it.amount || 0);
        if (it.currency) target.currency = it.currency;
      } else if (it.label) {
        extra.push({
          step_id: sid || `adhoc_${Date.now()}`,
          label: it.label,
          category: it.category || "other",
          amount: Number(it.amount || 0),
          currency: it.currency || "USDT",
          only_official_wallet: Boolean(it.only_official_wallet),
        });
      }
    });
    base.fee_entries = [...byId.values(), ...extra];
    base.conversions = (f.conversions || []).map((cv) => ({
      label: cv.label || `${cv.from_asset || "PRL"} → ${cv.to_asset || "USDT"}`,
      from_asset: cv.from_asset || "PRL",
      to_asset: cv.to_asset || "USDT",
      sold_amount: Number(cv.sold_amount || 0),
      unit_price: Number(cv.unit_price || 0),
      received_amount: Number(cv.received_amount || 0),
      market_ref_rate: Number(cv.market_ref_rate || 0),
    }));
    base.fx_rates_cny = Object.entries(f.fx_rates_cny || {}).map(([asset, rate]) => ({
      asset,
      rate: Number(rate || 0),
    }));
    Object.entries(f.residual_deltas || {}).forEach(([k, v]) => {
      base.residual_deltas[k] = Number(v || 0);
    });
    state.feeFormState = base;
    state.linkedTradeIds = Array.isArray(f.linked_trade_ids) ? f.linked_trade_ids : [];
    state.residualSource = f.residual_source || (state.linkedTradeIds.length ? "trades" : "direct");

    syncFeeWalletTypeLock();
    const summ = computeFeeSummaryClient(
      collectFeeFormPayload(),
      Number(document.getElementById("settle-cny")?.value || 0),
      Number(document.getElementById("settle-prl")?.value || 0)
    );
    if (summ.has_any) {
      setFeesPanelExpanded(true);
    }
    updateFeeLiveSummary();
  }

  function clearFeeFormPayload() {
    state.linkedTradeIds = [];
    state.residualSource = "direct";
    state.feeFormState = makeDefaultFeeFormState();
    renderFeeDynamicPanels();
    updateFeeLiveSummary();
  }

  function computeFeeSummaryClient(f, cnyAmount = 0, prlAmount = 0) {
    const cny = Math.max(0, Number(cnyAmount || 0));
    const prl = Math.max(0, Number(prlAmount || 0));
    const refPrlCny = Number(state.data?.active_wallet?.overview?.prl_price_cny || 0);
    const conversions = Array.isArray(f.conversions) ? f.conversions : [];

    const rateMap = { CNY: 1 };
    Object.entries(f.fx_rates_cny || {}).forEach(([k, v]) => {
      if (Number(v) > 0) rateMap[k] = Number(v);
    });
    const propagate = () => {
      for (let pass = 0; pass <= conversions.length; pass++) {
        let changed = false;
        conversions.forEach((cv) => {
          const price =
            Number(cv.unit_price || 0) > 0
              ? Number(cv.unit_price)
              : Number(cv.sold_amount || 0) > 0 && Number(cv.received_amount || 0) > 0
                ? Number(cv.received_amount) / Number(cv.sold_amount)
                : 0;
          if (!(price > 0)) return;
          if (!rateMap[cv.from_asset] && rateMap[cv.to_asset] > 0) {
            rateMap[cv.from_asset] = price * rateMap[cv.to_asset];
            changed = true;
          } else if (!rateMap[cv.to_asset] && rateMap[cv.from_asset] > 0) {
            rateMap[cv.to_asset] = rateMap[cv.from_asset] / price;
            changed = true;
          }
        });
        if (!changed) break;
      }
    };
    propagate();
    if (!rateMap.PRL) {
      if (cny > 0 && prl > 0) rateMap.PRL = cny / prl;
      else if (refPrlCny > 0) rateMap.PRL = refPrlCny;
      propagate();
    }

    const feeItems = [];
    const totalsByCurrency = {};
    let estFeeCny = 0;
    const unconverted = [];
    (f.fee_entries || []).forEach((it) => {
      const amt = Number(it.amount || 0);
      if (!(amt > 0)) return;
      const cur = (it.currency || "USDT").toUpperCase();
      totalsByCurrency[cur] = Number(((totalsByCurrency[cur] || 0) + amt).toFixed(6));
      const rc = rateMap[cur] || 0;
      const cnyEq = rc > 0 ? Number((amt * rc).toFixed(2)) : null;
      if (cnyEq != null) estFeeCny += cnyEq;
      else if (!unconverted.includes(cur)) unconverted.push(cur);
      feeItems.push({ step_id: it.step_id, label: it.label, category: it.category, amount: amt, unit: cur, cny_est: cnyEq });
    });
    estFeeCny = Number(estFeeCny.toFixed(2));

    let fxSpreadLossCny = 0;
    conversions.forEach((cv) => {
      const mkt = Number(cv.market_ref_rate || 0);
      const price =
        Number(cv.unit_price || 0) > 0
          ? Number(cv.unit_price)
          : Number(cv.sold_amount || 0) > 0 && Number(cv.received_amount || 0) > 0
            ? Number(cv.received_amount) / Number(cv.sold_amount)
            : 0;
      const sold = Number(cv.sold_amount || 0);
      const toR = rateMap[cv.to_asset] || 0;
      if (mkt > price && price > 0 && sold > 0 && toR > 0) {
        fxSpreadLossCny += sold * (mkt - price) * toR;
      }
    });
    fxSpreadLossCny = Number(fxSpreadLossCny.toFixed(2));

    const totalLossCny = Number((estFeeCny + fxSpreadLossCny).toFixed(2));
    const grossEst = cny > 0 && totalLossCny > 0 ? cny + totalLossCny : cny;
    const lossRatePct = grossEst > 0 && totalLossCny > 0 ? Number(((totalLossCny / grossEst) * 100).toFixed(2)) : 0;
    const resDeltas = f.residual_deltas || {};
    const hasRes = Object.values(resDeltas).some((v) => Math.abs(Number(v || 0)) > 1e-8);
    const hasAny = Boolean(
      feeItems.length ||
        conversions.length ||
        Object.keys(f.fx_rates_cny || {}).length ||
        hasRes ||
        (f.linked_trade_ids && f.linked_trade_ids.length)
    );

    return {
      has_any: hasAny,
      fees: f,
      fee_items: feeItems,
      totals_by_currency: totalsByCurrency,
      estimated_fee_cny: estFeeCny,
      fx_spread_loss_cny: fxSpreadLossCny,
      total_loss_cny: totalLossCny,
      unconverted_currencies: unconverted,
      loss_rate_pct: lossRatePct,
      net_cny_per_prl: cny > 0 && prl > 0 ? Number((cny / prl).toFixed(4)) : 0,
      conversions,
      residual_deltas: resDeltas,
    };
  }

  function updateFeeLiveSummary() {
    const f = collectFeeFormPayload();
    const cny = Number(document.getElementById("settle-cny")?.value || 0);
    const prl = Number(document.getElementById("settle-prl")?.value || 0);
    const summ = computeFeeSummaryClient(f, cny, prl);
    const rs = (state.data && state.data.residual_summary) || {};
    const poolBadge = document.getElementById("settle-dust-pool-badge");
    if (poolBadge) {
      poolBadge.textContent = formatDustPoolBadgeText(rs);
    }

    const colSummary = document.getElementById("fee-collapse-summary");
    if (colSummary) {
      if (!summ.has_any) {
        colSummary.textContent = "可展开按你自己的提现路径填写各环节手续费、兑换价格与残余变动（支持完全自定义流程步骤；填写后会自动计入微信分账账单）";
      } else {
        const parts = [];
        if (summ.fee_items.length) parts.push(`已填 ${summ.fee_items.length} 项手续费`);
        if (summ.conversions.length) parts.push(`${summ.conversions.length} 段兑换汇率`);
        if (summ.fx_spread_loss_cny > 0) parts.push(`汇损 ￥${fmtMoney(summ.fx_spread_loss_cny)}`);
        if (summ.total_loss_cny > 0) parts.push(`总损耗约 ￥${fmtMoney(summ.total_loss_cny)} (${summ.loss_rate_pct}%)`);
        const resParts = Object.entries(summ.residual_deltas || {}).map(([k, v]) => `${k}残余 ${formatSignedDust(v)}`);
        if (resParts.length) parts.push(resParts.join(" · "));
        if (f.linked_trade_ids.length) parts.push(`关联 ${f.linked_trade_ids.length} 笔流水`);
        colSummary.textContent = parts.join(" · ");
      }
    }

    const bar = document.getElementById("fee-live-summary-bar");
    if (!bar) return;
    if (!summ.has_any) {
      bar.innerHTML = "尚未填写可选手续费或成交价格；填写后将实时计算折合损耗率、单价与残余变动。";
      return;
    }

    const convBits = (summ.conversions || []).map((cv) => {
      const p =
        cv.unit_price > 0
          ? cv.unit_price
          : cv.sold_amount > 0 && cv.received_amount > 0
            ? Number((cv.received_amount / cv.sold_amount).toFixed(6))
            : 0;
      const mkt = cv.market_ref_rate > p && p > 0 ? ` (参考 ${cv.market_ref_rate})` : "";
      return `${escapeHtml(cv.from_asset)}→${escapeHtml(cv.to_asset)} <strong>${p || "--"}</strong>${escapeHtml(mkt)}`;
    });
    if (summ.net_cny_per_prl > 0) convBits.push(`实到净单价 <strong>￥${summ.net_cny_per_prl}/PRL</strong>`);

    const feeCurParts = Object.entries(summ.totals_by_currency)
      .filter(([, val]) => val > 0)
      .map(([u, val]) => `${fmtPrl(val)} ${u}`);

    const fxLoss = summ.fx_spread_loss_cny || 0;
    const totalLoss = Number((summ.total_loss_cny || 0).toFixed(2));
    const lossHtml =
      totalLoss > 0
        ? fxLoss > 0
          ? `（手续费 ￥${fmtMoney(summ.estimated_fee_cny)} + 汇损 <strong style="color:#fbbf24">￥${fmtMoney(fxLoss)}</strong> = 总损耗 <strong style="color:#fbbf24">￥${fmtMoney(totalLoss)}</strong>，占毛收益 <strong>${summ.loss_rate_pct}%</strong>）`
          : `（折合损耗约 <strong style="color:#fbbf24">￥${fmtMoney(summ.estimated_fee_cny)}</strong>，占毛收益 <strong>${summ.loss_rate_pct}%</strong>）`
        : "";
    const fxOnlyHtml =
      !feeCurParts.length && fxLoss > 0
        ? `<div>💱 汇率点差损耗：<strong style="color:#fbbf24">约 ￥${fmtMoney(fxLoss)}</strong>（占毛收益 <strong>${summ.loss_rate_pct}%</strong>）</div>`
        : "";
    const resStr = Object.entries(summ.residual_deltas || {})
      .map(([k, v]) => `${k} ${formatSignedDust(v)}`)
      .join(" · ");
    const linkedHtml = f.linked_trade_ids.length
      ? `<div>🔗 已关联 <strong>${f.linked_trade_ids.length}</strong> 笔待结算流水（保存提现时将自动核销归档）</div>`
      : "";

    bar.innerHTML = `
      ${convBits.length ? `<div>📈 成交单价 / 汇率：${convBits.join(" · ")}</div>` : ""}
      ${feeCurParts.length ? `<div>💸 流程手续费合计：<strong>${escapeHtml(feeCurParts.join(" + "))}</strong>${lossHtml}</div>` : fxOnlyHtml}
      ${resStr ? `<div>⚖️ 本轮精度残余变动：${escapeHtml(resStr)}</div>` : ""}
      ${linkedHtml}
    `;
  }

  function buildBillText(title, walletAlias, timeStr, cny, prl, totalDelta, allocs, feesObj = null, feeSummObj = null) {
    const lines = [
      `📊 Pearl(PRL) 挖矿分红明细 - ${title || walletAlias}`,
      `🕒 结算时间：${timeStr}`,
      `💰 实际到账：￥${fmtMoney(cny)}` + (prl > 0 ? ` (${fmtPrl(prl)} PRL)` : ""),
      `⚡ 区间总增量份额：${fmtInt(totalDelta)}`,
    ];

    const summ =
      feeSummObj ||
      (feesObj ? computeFeeSummaryClient(feesObj, cny, prl) : null);
    if (summ && summ.has_any) {
      lines.push(`————————————`);
      const rateBits = (summ.conversions || []).map((cv) => {
        const p =
          cv.unit_price > 0
            ? cv.unit_price
            : cv.sold_amount > 0 && cv.received_amount > 0
              ? Number((cv.received_amount / cv.sold_amount).toFixed(6))
              : 0;
        const mkt = cv.market_ref_rate > p && p > 0 ? `(参考${cv.market_ref_rate})` : "";
        return `${cv.from_asset}→${cv.to_asset} ${p}${mkt}`;
      });
      if (summ.net_cny_per_prl > 0) rateBits.push(`净均价 ￥${summ.net_cny_per_prl}/PRL`);
      if (rateBits.length) {
        lines.push(`💱 成交汇率：${rateBits.join(" · ")}`);
      }
      if (summ.fee_items && summ.fee_items.length) {
        const feeDesc = summ.fee_items
          .map((it) => `${it.label} ${fmtPrl(it.amount)} ${it.unit}${it.cny_est != null ? `(≈￥${fmtMoney(it.cny_est)})` : ""}`)
          .join("；");
        lines.push(`🧾 流程手续费：${feeDesc}`);
      }
      const fxLoss = summ.fx_spread_loss_cny || 0;
      const totalLoss = Number((summ.total_loss_cny ?? summ.estimated_fee_cny ?? 0).toFixed(2));
      if (fxLoss > 0 && summ.estimated_fee_cny > 0) {
        lines.push(
          `📉 总损耗：约 ￥${fmtMoney(totalLoss)}（手续费 ￥${fmtMoney(summ.estimated_fee_cny)} + 汇损 ￥${fmtMoney(fxLoss)}，损耗率 ${summ.loss_rate_pct}%）`
        );
      } else if (fxLoss > 0) {
        lines.push(`📉 汇率点差损耗：约 ￥${fmtMoney(fxLoss)} (损耗率 ${summ.loss_rate_pct}%)`);
      } else if (summ.estimated_fee_cny > 0) {
        lines.push(`📉 手续费合计损耗：约 ￥${fmtMoney(summ.estimated_fee_cny)} (损耗率 ${summ.loss_rate_pct}%)`);
      }
      const dustBits = Object.entries(summ.residual_deltas || {})
        .filter(([, v]) => Math.abs(Number(v || 0)) > 1e-8)
        .map(([k, v]) => `${k} ${formatSignedDust(v)}`);
      if (dustBits.length) {
        lines.push(`⚖️ 交易精度残余变动：${dustBits.join(" | ")}（留存交易所待后续消耗）`);
      }
    }

    lines.push(`————————————`);
    allocs.forEach((a) => {
      lines.push(
        `• ${a.member_name}：+${fmtInt(a.delta_shares)} 份额 (${(a.ratio * 100).toFixed(2)}%) → 应得 ￥${fmtMoney(a.cny_earned)}`
      );
    });
    return lines.join("\n");
  }

  // -------------------------------------------------------------------
  // Ledger & Pool Payments
  // -------------------------------------------------------------------
  function renderLedger() {
    const { checkpoints, active_wallet } = state.data;
    const listBox = document.getElementById("checkpoints-list");
    if (!checkpoints || !checkpoints.length) {
      listBox.innerHTML = `<div class="empty-state">暂无提现或基准快照记录，前往「提现结算 &amp; 分红」记录第一笔！</div>`;
    } else {
      listBox.innerHTML = checkpoints
        .map((cp) => {
          const isBase = cp.kind === "baseline";
          const chips = isBase
            ? (cp.workers || [])
                .map(
                  (w) => `
                  <div class="cp-chip" style="border-left: 3px solid var(--blue)">
                    <strong>${escapeHtml(w.worker_name)}</strong> (${escapeHtml(w.member_name_snapshot)})：${fmtInt(w.curr_shares)}
                  </div>
                `
                )
                .join("")
            : (cp.allocations || [])
                .map(
                  (a) => `
                  <div class="cp-chip" style="border-left: 3px solid ${escapeHtml(a.member_color)}">
                    <strong>${escapeHtml(a.member_name)}</strong>：+${fmtInt(a.delta_shares)} (${(a.ratio * 100).toFixed(2)}%) →
                    <strong style="color:#34d399">￥${fmtMoney(a.cny_earned)}</strong>
                  </div>
                `
                )
                .join("");

          const workerDrawerRows = !isBase
            ? (cp.workers || [])
                .map(
                  (w) => `
                  <div class="cp-chip" style="border-left: 2px solid var(--border-light)">
                    <strong>${escapeHtml(w.worker_name)}</strong> (${escapeHtml(w.member_name_snapshot)})：
                    ${fmtInt(w.prev_shares)} → ${fmtInt(w.curr_shares)}
                    (<span style="color:#34d399">+${fmtInt(w.delta_shares)}</span>)
                  </div>
                `
                )
                .join("")
            : "";

          const fs = cp.fee_summary || {};
          const fobj = cp.fees || {};
          let feeMetaHtml = "";
          if (!isBase && fs.has_any) {
            const bits = [];
            (fs.conversions || []).forEach((cv) => {
              if (cv.unit_price > 0) bits.push(`${escapeHtml(cv.from_asset)}→${escapeHtml(cv.to_asset)} ${cv.unit_price}`);
            });
            if (fs.fee_items && fs.fee_items.length) {
              const feeListStr = fs.fee_items.map((x) => `${escapeHtml(x.label)} ${fmtPrl(x.amount)} ${escapeHtml(x.unit)}`).join(" · ");
              bits.push(`手续费[${feeListStr}]`);
            }
            if ((fs.fx_spread_loss_cny || 0) > 0) {
              bits.push(`<span style="color:#fbbf24">汇损 ￥${fmtMoney(fs.fx_spread_loss_cny)}</span>`);
            }
            const totalCpLoss = fs.total_loss_cny ?? fs.estimated_fee_cny ?? 0;
            if (totalCpLoss > 0) {
              bits.push(`<span style="color:#fbbf24">总损耗 ￥${fmtMoney(totalCpLoss)} (${fs.loss_rate_pct}%)</span>`);
            }
            Object.entries(fobj.residual_deltas || {}).forEach(([k, v]) => {
              if (Math.abs(Number(v || 0)) > 1e-8) {
                bits.push(`${escapeHtml(k)}残余 ${escapeHtml(formatSignedDust(v))}`);
              }
            });
            feeMetaHtml = `<div style="margin-top:6px;font-size:12px;color:var(--text-muted);">${bits.join(" · ")}</div>`;
          }

          return `
            <div class="cp-card">
              <div class="cp-card-head">
                <div class="cp-title-row">
                  <span class="status-badge ${isBase ? "status-manual" : "status-online"}">${isBase ? "📌 基准起点" : "💰 提现结算"}</span>
                  <strong>${escapeHtml(cp.checkpoint_time)}</strong>
                  ${cp.label ? `<span>· ${escapeHtml(cp.label)}</span>` : ""}
                  <span style="color:var(--text-dim);font-size:12px;">${isBase ? `记录 ${(cp.workers || []).length} 台矿机` : `增量 +${fmtInt(cp.total_delta_shares)} 份额`}</span>
                  ${cp.pool_paid_snapshot != null ? `<span style="color:var(--text-dim);font-size:11.5px;">· 矿池已付快照 ${Number(cp.pool_paid_snapshot).toFixed(4)} PRL</span>` : ""}
                </div>
                <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                  ${!isBase ? `<span class="cp-money">￥${fmtMoney(cp.cny_amount)}</span>` : ""}
                  ${cp.prl_amount > 0 ? `<span style="color:var(--text-muted)">(${fmtPrl(cp.prl_amount)} PRL)</span>` : ""}
                  ${!isBase && (cp.workers || []).length ? `<button class="link-btn" data-cp-act="toggle-workers" data-cp-id="${cp.id}">矿机明细(${(cp.workers || []).length})</button>` : ""}
                  <button class="link-btn" data-cp-act="edit-pool-snap" data-cp-id="${cp.id}">改已付快照</button>
                  <button class="link-btn" data-cp-act="sync-cp" data-cp-id="${cp.id}">同步</button>
                  ${isBase ? `<button class="link-btn" data-cp-act="edit-base" data-cp-id="${cp.id}">改矿机份额</button>` : ""}
                  ${!isBase ? `<button class="link-btn" data-cp-act="edit" data-cp-id="${cp.id}">改金额</button>` : ""}
                  ${!isBase ? `<button class="link-btn" data-cp-act="copy" data-cp-id="${cp.id}">复制账单</button>` : ""}
                  <button class="link-btn danger" data-cp-act="del" data-cp-id="${cp.id}">删除</button>
                </div>
              </div>
              ${cp.notes ? `<div style="margin-top:6px;font-size:12px;color:var(--text-muted);">备注：${escapeHtml(cp.notes)}</div>` : ""}
              ${feeMetaHtml}
              ${chips ? `<div class="cp-alloc-chips">${chips}</div>` : ""}
              ${workerDrawerRows ? `<div id="cp-workers-${cp.id}" class="cp-workers-drawer hidden"><div style="font-size:11.5px;color:var(--text-muted);margin-bottom:6px;">该笔结算锁定的各矿机份额快照（上次 → 本次）：</div><div class="cp-alloc-chips" style="margin-top:0;">${workerDrawerRows}</div></div>` : ""}
            </div>
          `;
        })
        .join("");

      listBox.querySelectorAll("[data-cp-act='toggle-workers']").forEach((b) => {
        b.onclick = () => {
          const drawer = document.getElementById(`cp-workers-${b.dataset.cpId}`);
          if (drawer) drawer.classList.toggle("hidden");
        };
      });

      listBox.querySelectorAll("[data-cp-act='copy']").forEach((b) => {
        b.onclick = () => {
          const cp = checkpoints.find((c) => c.id === Number(b.dataset.cpId));
          if (!cp) return;
          const text = buildBillText(
            cp.label || active_wallet.alias,
            active_wallet.alias,
            cp.checkpoint_time,
            cp.cny_amount,
            cp.prl_amount,
            cp.total_delta_shares,
            cp.allocations || [],
            cp.fees || null,
            cp.fee_summary || null
          );
          copyText(text, "已复制该笔提现微信分账账单");
        };
      });

      listBox.querySelectorAll("[data-cp-act='edit-pool-snap']").forEach((b) => {
        b.onclick = () => {
          const cp = checkpoints.find((c) => c.id === Number(b.dataset.cpId));
          if (cp) openCalibratePoolPaidModal(cp);
        };
      });

      listBox.querySelectorAll("[data-cp-act='sync-cp']").forEach((b) => {
        b.onclick = () => {
          const cp = checkpoints.find((c) => c.id === Number(b.dataset.cpId));
          if (cp) openSyncRecordsModal("export", [cp.id]);
        };
      });

      listBox.querySelectorAll("[data-cp-act='edit-base']").forEach((b) => {
        b.onclick = () => {
          const cp = checkpoints.find((c) => c.id === Number(b.dataset.cpId));
          if (cp) openHistoricalBaselineModal(cp);
        };
      });

      listBox.querySelectorAll("[data-cp-act='edit']").forEach((b) => {
        b.onclick = () => {
          const cp = checkpoints.find((c) => c.id === Number(b.dataset.cpId));
          if (!cp) return;
          openEditCheckpointModal(cp);
        };
      });

      listBox.querySelectorAll("[data-cp-act='del']").forEach((b) => {
        b.onclick = async () => {
          const ok = await confirmDialog("确定删除这条提现/基准记录吗？删除后不可恢复。", {
            title: "删除账本记录",
            confirmText: "确认删除",
            danger: true,
          });
          if (!ok) return;
          const next = await api().delete_checkpoint(active_wallet.id, Number(b.dataset.cpId));
          state.settleDraft = null;
          applyStatePayload(next);
          toast("已删除记录");
        };
      });
    }

    // Pool payments table (with multi-select & merge settlement)
    const ov = (active_wallet && active_wallet.overview) || {};
    const payments = ov.payments || [];
    const prlPrice = Number(ov.prl_price_cny || 0);
    const latestCpTime = checkpoints && checkpoints.length > 0 ? checkpoints[0].checkpoint_time : "";
    const pBody = document.getElementById("pool-payments-tbody");
    const checkAllEl = document.getElementById("pay-check-all");
    const summaryEl = document.getElementById("pay-merge-summary");
    const mergeBtn = document.getElementById("btn-pay-merge-settle");
    const selectUnsettledBtn = document.getElementById("btn-pay-select-unsettled");

    // Clean out out-of-range indices
    Array.from(state.selectedPaymentIndices).forEach((idx) => {
      if (idx < 0 || idx >= payments.length) state.selectedPaymentIndices.delete(idx);
    });

    const syncPaymentMergeBar = () => {
      const sel = Array.from(state.selectedPaymentIndices)
        .sort((a, b) => a - b)
        .map((i) => payments[i])
        .filter(Boolean);
      const count = sel.length;
      const sumPrl = sel.reduce((acc, p) => acc + Number(p.amount_prl || 0), 0);
      if (summaryEl) {
        if (count === 0) {
          summaryEl.innerHTML = "未勾选打款记录（可勾选多笔合并结算）";
        } else {
          const estCny = prlPrice > 0 ? ` (≈￥${(sumPrl * prlPrice).toFixed(2)})` : "";
          summaryEl.innerHTML = `已选 <strong>${count}</strong> 笔 · 合计 <strong style="color:#34d399">${sumPrl.toFixed(6)} PRL</strong>${escapeHtml(estCny)}`;
        }
      }
      if (mergeBtn) {
        mergeBtn.disabled = count === 0;
        mergeBtn.textContent = count > 1 ? `💰 合并所选 ${count} 笔去结算` : "💰 以所选打款去结算";
      }
      if (checkAllEl) {
        checkAllEl.checked = payments.length > 0 && count === payments.length;
      }
    };

    if (!payments.length) {
      pBody.innerHTML = `<tr><td colspan="6" class="empty-state">暂未获取到矿池转账记录</td></tr>`;
      syncPaymentMergeBar();
    } else {
      pBody.innerHTML = payments
        .map((p, i) => {
          const isNew = !latestCpTime || p.paid_at > latestCpTime;
          const badge = isNew
            ? `<span class="status-badge status-online">本轮新打款</span>`
            : `<span class="status-badge status-offline">早于上次基准</span>`;
          const checked = state.selectedPaymentIndices.has(i) ? "checked" : "";
          return `
          <tr>
            <td style="text-align:center;">
              <input type="checkbox" class="pay-row-check" data-pay-check="${i}" ${checked} />
            </td>
            <td>${escapeHtml(p.paid_at)}</td>
            <td>${badge}</td>
            <td class="num"><strong>${Number(p.amount_prl).toFixed(6)} PRL</strong></td>
            <td><code class="mono-addr">${escapeHtml(p.tx_hash.slice(0, 18))}...${escapeHtml(p.tx_hash.slice(-10))}</code></td>
            <td class="act">
              <button class="link-btn" data-pay-idx="${i}">单笔去结算</button>
              ${p.explorer_url ? `<button class="link-btn" data-open-tx="${escapeHtml(p.explorer_url)}">浏览器查看 ↗</button>` : ""}
            </td>
          </tr>
        `;
        })
        .join("");

      pBody.querySelectorAll(".pay-row-check").forEach((chk) => {
        chk.onchange = () => {
          const idx = Number(chk.dataset.payCheck);
          if (chk.checked) {
            state.selectedPaymentIndices.add(idx);
          } else {
            state.selectedPaymentIndices.delete(idx);
          }
          syncPaymentMergeBar();
        };
      });

      if (checkAllEl) {
        checkAllEl.onchange = () => {
          state.selectedPaymentIndices.clear();
          if (checkAllEl.checked) {
            payments.forEach((_, idx) => state.selectedPaymentIndices.add(idx));
          }
          renderLedger();
        };
      }

      if (selectUnsettledBtn) {
        selectUnsettledBtn.onclick = () => {
          state.selectedPaymentIndices.clear();
          payments.forEach((p, idx) => {
            if (!latestCpTime || p.paid_at > latestCpTime) {
              state.selectedPaymentIndices.add(idx);
            }
          });
          renderLedger();
          const cnt = state.selectedPaymentIndices.size;
          if (cnt > 0) {
            toast(`已自动勾选上次结算基准后的 ${cnt} 笔新打款`);
          } else {
            toast("没有晚于上次结算基准的新打款记录，您可手动勾选需要合并的记录", "warn");
          }
        };
      }

      if (mergeBtn) {
        mergeBtn.onclick = async () => {
          const sel = Array.from(state.selectedPaymentIndices)
            .sort((a, b) => a - b)
            .map((i) => payments[i])
            .filter(Boolean);
          const payload = buildMergedPaymentsPayload(sel);
          if (!payload) return;
          switchView("settle");
          await initSettleDraft(payload);
          toast(
            payload.count > 1
              ? `已合并 ${payload.count} 笔矿池打款（共 ${payload.prl.toFixed(6)} PRL），请输入实收人民币`
              : `已带入所选矿池打款（${payload.prl.toFixed(6)} PRL），请输入实收人民币`
          );
        };
      }

      pBody.querySelectorAll("[data-pay-idx]").forEach((b) => {
        b.onclick = async () => {
          const p = payments[Number(b.dataset.payIdx)];
          const payload = buildMergedPaymentsPayload([p]);
          switchView("settle");
          await initSettleDraft(payload);
          toast("已自动带入矿池提现时间与 PRL 数量，请输入实收人民币");
        };
      });
      pBody.querySelectorAll("[data-open-tx]").forEach((b) => {
        b.onclick = () => api().open_url(b.dataset.openTx);
      });

      syncPaymentMergeBar();
    }

    renderExchangeTrades();
  }

  function buildFeesFromSelectedTrades(trades) {
    const selected = Array.isArray(trades) ? trades : [];
    let prlTotal = 0;
    let totalTransferPrl = 0;
    const feeEntries = [];
    const conversions = [];
    const residualDeltas = {};

    selected.forEach((t, idx) => {
      const fromA = (t.from_asset || "PRL").toUpperCase();
      const toA = (t.to_asset || "USDT").toUpperCase();
      const venue = t.venue || "交易所";
      if (fromA === "PRL") {
        prlTotal += Number(t.source_amount || t.sold_amount || 0);
      }
      totalTransferPrl += Number(t.transfer_fee || 0);
      const tFee = Number(t.trade_fee || 0);
      if (tFee > 0) {
        feeEntries.push({
          step_id: `trade_fee_${t.id || idx}`,
          label: `${venue} ${fromA}→${toA} 交易费`,
          category: "trade",
          amount: tFee,
          currency: (t.trade_fee_unit || toA).toUpperCase(),
          only_official_wallet: false,
        });
      }
      if (Number(t.sold_amount || 0) > 0 || Number(t.receive_net || 0) > 0) {
        conversions.push({
          label: `${venue} (${fromA}→${toA})`,
          from_asset: fromA,
          to_asset: toA,
          sold_amount: Number(t.sold_amount || 0),
          unit_price: Number(t.unit_price || 0),
          received_amount: Number(t.receive_net || 0),
          market_ref_rate: 0,
        });
      }
      const dAsset = (t.dust_asset || fromA).toUpperCase();
      const dVal = Number(t.dust_delta || 0);
      if (Math.abs(dVal) > 1e-8) {
        residualDeltas[dAsset] = Number(((residualDeltas[dAsset] || 0) + dVal).toFixed(6));
      }
    });

    if (totalTransferPrl > 0) {
      feeEntries.unshift({
        step_id: "wallet_to_exchange",
        label: "官方钱包转交易所",
        category: "transfer",
        amount: Number(totalTransferPrl.toFixed(6)),
        currency: "PRL",
        only_official_wallet: true,
      });
    }

    return {
      prlTotal: Number(prlTotal.toFixed(6)),
      fees: {
        schema_version: 2,
        fee_entries: feeEntries,
        conversions,
        fx_rates_cny: {},
        residual_deltas: residualDeltas,
        residual_source: "trades",
        linked_trade_ids: selected.map((t) => Number(t.id)),
      },
    };
  }

  function renderExchangeTrades() {
    const trades = (state.data && state.data.exchange_trades) || [];
    const rs = (state.data && state.data.residual_summary) || {};
    const rsBals = rs.balances || {};

    const prlBadge = document.getElementById("ledger-dust-prl-badge");
    const usdtBadge = document.getElementById("ledger-dust-usdt-badge");
    const feeBadge = document.getElementById("ledger-fee-loss-badge");
    if (prlBadge) prlBadge.textContent = `PRL 残余池: ${fmtPrl(rsBals.PRL ?? rs.prl_dust_balance ?? 0)} PRL`;
    if (usdtBadge) usdtBadge.textContent = `USDT 残余池: ${fmtPrl(rsBals.USDT ?? rs.usdt_dust_balance ?? 0)} USDT`;
    if (feeBadge) feeBadge.textContent = `已结算累计损耗: ￥${fmtMoney(rs.total_estimated_fee_cny || 0)}`;

    const validIds = new Set(trades.filter((t) => !t.linked_checkpoint_id).map((t) => t.id));
    Array.from(state.selectedTradeIds).forEach((id) => {
      if (!validIds.has(id)) state.selectedTradeIds.delete(id);
    });

    const tbody = document.getElementById("exchange-trades-tbody");
    const mergeBtn = document.getElementById("btn-merge-trades-settle");
    const checkAllEl = document.getElementById("trade-check-all");
    if (!tbody) return;

    if (!trades.length) {
      tbody.innerHTML = `<tr><td colspan="12" class="empty-state">暂无交易所卖币 / 换汇流水；收到矿池打款后可随时点右上角「+ 记一笔卖币 / 换汇」记录单价与残余。</td></tr>`;
      if (mergeBtn) mergeBtn.disabled = true;
      return;
    }

    tbody.innerHTML = trades
      .map((t) => {
        const srcUnit = (t.from_asset || "PRL").toUpperCase();
        const dstUnit = (t.to_asset || "USDT").toUpperCase();
        const venue = t.venue || "交易所";
        const dustAsset = (t.dust_asset || srcUnit).toUpperCase();
        const settled = t.linked_checkpoint_id != null;
        const checked = !settled && state.selectedTradeIds.has(t.id);
        const delta = Number(t.dust_delta || 0);
        const deltaCls = delta > 1e-8 ? "dust-delta-pos" : delta < -1e-8 ? "dust-delta-neg" : "dust-delta-zero";
        const deltaHint =
          delta > 1e-8
            ? `${formatSignedDust(delta, dustAsset)} (留存)`
            : delta < -1e-8
              ? `${formatSignedDust(delta, dustAsset)} (消耗旧余)`
              : `0 (无残余)`;
        const statusTag = settled
          ? `<span class="status-badge status-manual">已入账结算 #${t.linked_checkpoint_id}</span>`
          : `<span class="status-badge status-online">待结算</span>`;

        return `
          <tr>
            <td style="text-align:center;">
              <input type="checkbox" class="trade-row-check" data-tid="${t.id}" ${settled ? "disabled" : ""} ${checked ? "checked" : ""} />
            </td>
            <td class="mono">${escapeHtml(t.trade_time)}</td>
            <td>${escapeHtml(t.wallet_alias || "全局/通用")}</td>
            <td><strong>${escapeHtml(venue)} · ${escapeHtml(srcUnit)}→${escapeHtml(dstUnit)}</strong></td>
            <td class="num">${t.source_amount > 0 ? `${fmtPrl(t.source_amount)} ${escapeHtml(srcUnit)}${t.transfer_fee > 0 ? ` (转出费-${fmtPrl(t.transfer_fee)})` : ""}` : "--"}</td>
            <td class="num"><strong>${fmtPrl(t.sold_amount)} ${escapeHtml(srcUnit)}</strong></td>
            <td class="num">${t.unit_price > 0 ? `${t.unit_price} ${escapeHtml(dstUnit)}/${escapeHtml(srcUnit)}` : "--"}</td>
            <td class="num">${t.trade_fee > 0 ? `${fmtPrl(t.trade_fee)} ${escapeHtml(t.trade_fee_unit || dstUnit)}` : "--"}</td>
            <td class="num"><strong style="color:#34d399">${fmtPrl(t.receive_net)} ${escapeHtml(dstUnit)}</strong></td>
            <td class="num"><span class="${deltaCls}">${escapeHtml(deltaHint)}</span></td>
            <td>${statusTag}${t.notes ? ` <span class="c-muted">· ${escapeHtml(t.notes)}</span>` : ""}</td>
            <td class="act">
              <button class="link-btn danger" data-del-trade="${t.id}">删除</button>
            </td>
          </tr>
        `;
      })
      .join("");

    if (mergeBtn) mergeBtn.disabled = state.selectedTradeIds.size === 0;
    if (checkAllEl) {
      const unsettled = trades.filter((t) => t.linked_checkpoint_id == null);
      checkAllEl.checked = unsettled.length > 0 && state.selectedTradeIds.size === unsettled.length;
      checkAllEl.onchange = () => {
        state.selectedTradeIds.clear();
        if (checkAllEl.checked) {
          unsettled.forEach((t) => state.selectedTradeIds.add(t.id));
        }
        renderExchangeTrades();
      };
    }

    tbody.querySelectorAll(".trade-row-check").forEach((chk) => {
      chk.onchange = () => {
        const id = Number(chk.dataset.tid);
        if (chk.checked) state.selectedTradeIds.add(id);
        else state.selectedTradeIds.delete(id);
        if (mergeBtn) mergeBtn.disabled = state.selectedTradeIds.size === 0;
      };
    });

    tbody.querySelectorAll("[data-del-trade]").forEach((b) => {
      b.onclick = async () => {
        const ok = await confirmDialog("确定删除这条交易流水记录吗？残余池余额将自动重新计算。", {
          title: "删除交易记录",
          confirmText: "删除",
          danger: true,
        });
        if (!ok) return;
        const next = await api().delete_exchange_trade(Number(b.dataset.delTrade), state.data?.active_wallet?.id || null);
        applyStatePayload(next);
        toast("已删除交易流水并更新残余池");
      };
    });
  }

  function openImportUnsettledTradesModal() {
    const trades = ((state.data && state.data.exchange_trades) || []).filter((t) => t.linked_checkpoint_id == null);
    if (!trades.length) {
      toast("当前没有待结算的交易流水（可先点「记一笔卖币/换汇」记录单笔卖出订单）", "warn");
      return;
    }
    const rowsHtml = trades
      .map((t) => {
        const srcU = (t.from_asset || "PRL").toUpperCase();
        const dstU = (t.to_asset || "USDT").toUpperCase();
        const venue = t.venue || "交易所";
        const preChecked = state.linkedTradeIds.includes(t.id) ? "checked" : "";
        return `
          <label style="display:flex;align-items:center;gap:10px;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg-elevated);cursor:pointer;">
            <input type="checkbox" class="imp-trade-chk" value="${t.id}" ${preChecked} />
            <div style="flex:1;font-size:12.5px;">
              <div><strong>${escapeHtml(venue)} · ${escapeHtml(srcU)}→${escapeHtml(dstU)}</strong> · <span class="mono">${escapeHtml(t.trade_time)}</span></div>
              <div class="c-muted">卖出 ${fmtPrl(t.sold_amount)} ${escapeHtml(srcU)} @ ${t.unit_price || "--"} → 实得 ${fmtPrl(t.receive_net)} ${escapeHtml(dstU)} · 残余 ${escapeHtml(formatSignedDust(t.dust_delta, t.dust_asset || srcU))}</div>
            </div>
          </label>
        `;
      })
      .join("");

    openModal(
      "选择要合并入本次提现结算的卖币/换汇流水",
      `
        <p class="panel-desc">支持勾选多次攒币卖出订单合并入同一笔人民币分红，系统会自动汇总数量、兑换价格、手续费与残余变动。</p>
        <div style="display:flex;flex-direction:column;gap:8px;max-height:320px;overflow-y:auto;">
          ${rowsHtml}
        </div>
      `,
      [
        { text: "取消", onClick: closeModal },
        {
          text: "导入所选流水数据",
          cls: "btn-primary",
          onClick: () => {
            const ids = Array.from(document.querySelectorAll(".imp-trade-chk:checked")).map((c) => Number(c.value));
            if (!ids.length) return toast("请至少勾选一笔流水", "warn");
            const picked = trades.filter((t) => ids.includes(t.id));
            const built = buildFeesFromSelectedTrades(picked);
            if (built.prlTotal > 0 && document.getElementById("settle-prl")) {
              document.getElementById("settle-prl").value = built.prlTotal;
            }
            applyFeeFormPayload(built.fees);
            setFeesPanelExpanded(true);
            recomputeSettlePreview();
            closeModal();
            toast(`已导入 ${picked.length} 笔交易流水到本次结算`);
          },
        },
      ]
    );
  }

  function openAddExchangeTradeModal() {
    const aw = state.data && state.data.active_wallet;
    const wallets = (state.data && state.data.wallets) || [];
    const rs = (state.data && state.data.residual_summary) || {};
    const walletOpts = [`<option value="">全局/未指定钱包</option>`]
      .concat(
        wallets.map(
          (w) =>
            `<option value="${w.id}" ${aw && w.id === aw.id ? "selected" : ""}>${escapeHtml(w.alias)} (${w.wallet_type === "exchange" ? "交易所" : "官方"})</option>`
        )
      )
      .join("");

    openModal(
      "记一笔卖币 / 换汇流水",
      `
        <div class="alert-box" style="margin-bottom:4px;">
          ${escapeHtml(formatDustPoolBadgeText(rs))}（支持输入负残余消耗前期积累的最小单位零头）
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
          <div class="form-field">
            <label>快捷交易对预设</label>
            <select id="tr-pair-preset" class="input">
              <option value="PRL:USDT">交易所卖出 PRL → USDT</option>
              <option value="PRL:CNY">场外/直卖 PRL → CNY</option>
              <option value="USDT:CNY">出金换汇 USDT → CNY</option>
              <option value="USDT:USD">换汇 USDT → USD</option>
              <option value="USDT:EUR">换汇 USDT → EUR</option>
              <option value="CUSTOM">自定义其他交易对...</option>
            </select>
          </div>
          <div class="form-field">
            <label>交易场所 / 渠道名称</label>
            <input id="tr-venue" class="input" value="交易所" placeholder="例如：交易所 / C2C / 换汇平台" />
          </div>
          <div class="form-field">
            <label>卖出币种</label>
            <input id="tr-from-asset" class="input mono-addr" value="PRL" style="text-transform:uppercase;" />
          </div>
          <div class="form-field">
            <label>买入币种</label>
            <input id="tr-to-asset" class="input mono-addr" value="USDT" style="text-transform:uppercase;" />
          </div>
          <div class="form-field">
            <label>关联矿池钱包</label>
            <select id="tr-wallet" class="input">${walletOpts}</select>
          </div>
          <div class="form-field">
            <label>交易时间</label>
            <input id="tr-time" class="input" value="${escapeHtml(nowLocalStr())}" />
          </div>
          <div class="form-field" id="tr-transfer-fee-wrap">
            <label>官方钱包转出手续费 (PRL · 交易所钱包免填)</label>
            <input id="tr-transfer-fee" type="number" step="0.000001" min="0" class="input" placeholder="0" />
          </div>
          <div class="form-field">
            <label id="tr-source-lbl">原始转入/可用数量 (可选)</label>
            <input id="tr-source" type="number" step="0.000001" min="0" class="input" placeholder="用于自动计算精度残余" />
          </div>
          <div class="form-field">
            <label id="tr-sold-lbl">实际卖出数量 <span class="req">*</span></label>
            <input id="tr-sold" type="number" step="0.000001" min="0" class="input" placeholder="例如 5.54" />
          </div>
          <div class="form-field">
            <label id="tr-price-lbl">成交单价</label>
            <input id="tr-price" type="number" step="0.000001" min="0" class="input" placeholder="1 卖出币种 兑多少 买入币种" />
          </div>
          <div class="form-field">
            <label>交易手续费与币种</label>
            <div class="fee-unit-row">
              <input id="tr-fee" type="number" step="0.000001" min="0" class="input" placeholder="0" />
              <input id="tr-fee-unit" type="text" class="input fee-unit-sel mono-addr" value="USDT" style="text-transform:uppercase;" />
            </div>
          </div>
          <div class="form-field">
            <label id="tr-net-lbl">实际净得 (可自动算)</label>
            <input id="tr-net" type="number" step="0.000001" min="0" class="input" placeholder="留空按 卖出×单价-手续费 计算" />
          </div>
          <div class="form-field" style="grid-column:1 / -1;">
            <label>本笔精度残余变动 (+留存 / -消耗旧余)</label>
            <input id="tr-dust" type="number" step="0.000001" class="input" placeholder="留空按 (转入 - 转出费 - 卖出) 自动算" />
          </div>
        </div>
        <div class="form-field">
          <label>备注 (可选)</label>
          <input id="tr-notes" class="input" placeholder="例如：攒了两笔矿池打款统一卖出" />
        </div>
      `,
      [
        { text: "取消", onClick: closeModal },
        {
          text: "保存流水记录",
          cls: "btn-primary",
          onClick: async () => {
            const sold = Number(document.getElementById("tr-sold").value || 0);
            if (!(sold > 0)) return toast("请填写实际卖出数量", "error");
            const dustRaw = document.getElementById("tr-dust").value.trim();
            const fromAsset = document.getElementById("tr-from-asset").value.trim().toUpperCase() || "PRL";
            const toAsset = document.getElementById("tr-to-asset").value.trim().toUpperCase() || "USDT";
            try {
              const res = await api().add_exchange_trade(
                {
                  wallet_id: document.getElementById("tr-wallet").value || null,
                  venue: document.getElementById("tr-venue").value.trim() || "交易所",
                  from_asset: fromAsset,
                  to_asset: toAsset,
                  trade_time: document.getElementById("tr-time").value || nowLocalStr(),
                  source_amount: Number(document.getElementById("tr-source").value || 0),
                  transfer_fee: Number(document.getElementById("tr-transfer-fee").value || 0),
                  sold_amount: sold,
                  unit_price: Number(document.getElementById("tr-price").value || 0),
                  trade_fee: Number(document.getElementById("tr-fee").value || 0),
                  trade_fee_unit: document.getElementById("tr-fee-unit").value.trim().toUpperCase() || toAsset,
                  receive_net: Number(document.getElementById("tr-net").value || 0),
                  dust_delta: dustRaw === "" ? null : Number(dustRaw),
                  notes: document.getElementById("tr-notes").value,
                },
                aw ? aw.id : null
              );
              closeModal();
              applyStatePayload(res.state);
              toast("已记录流水并更新残余池");
            } catch (e) {
              toast(`保存流水失败: ${e.message || e}`, "error");
            }
          },
        },
      ]
    );

    const syncTradeModalFields = () => {
      const fromA = document.getElementById("tr-from-asset").value.trim().toUpperCase() || "PRL";
      const toA = document.getElementById("tr-to-asset").value.trim().toUpperCase() || "USDT";
      const widVal = Number(document.getElementById("tr-wallet").value || 0);
      const wObj = wallets.find((x) => x.id === widVal);
      const isOfficialWallet = !wObj || wObj.wallet_type !== "exchange";
      const tfWrap = document.getElementById("tr-transfer-fee-wrap");
      const tfInp = document.getElementById("tr-transfer-fee");
      if (tfWrap && tfInp) {
        const allowTf = fromA === "PRL" && isOfficialWallet;
        tfInp.disabled = !allowTf;
        if (!allowTf) tfInp.value = "";
        tfWrap.classList.toggle("fee-field-locked", !allowTf);
      }
      document.getElementById("tr-source-lbl").textContent = `原始转入/可用数量 (${fromA}，可选)`;
      document.getElementById("tr-sold-lbl").innerHTML = `实际卖出数量 (${fromA}) <span class="req">*</span>`;
      document.getElementById("tr-price-lbl").textContent = `成交单价 (${toA}/${fromA})`;
      document.getElementById("tr-net-lbl").textContent = `实际净得 (${toA}，可自动算)`;
    };
    document.getElementById("tr-pair-preset").onchange = (e) => {
      const val = e.target.value;
      if (val && val.includes(":")) {
        const [fa, ta] = val.split(":");
        document.getElementById("tr-from-asset").value = fa;
        document.getElementById("tr-to-asset").value = ta;
        document.getElementById("tr-fee-unit").value = ta;
      }
      syncTradeModalFields();
    };
    document.getElementById("tr-from-asset").oninput = syncTradeModalFields;
    document.getElementById("tr-to-asset").oninput = syncTradeModalFields;
    document.getElementById("tr-wallet").onchange = syncTradeModalFields;
    syncTradeModalFields();
  }

  function openCalibrateDustModal() {
    const rs = (state.data && state.data.residual_summary) || {};
    const rsBals = rs.balances || { PRL: 0, USDT: 0 };
    const rows = Array.from(new Set(["PRL", "USDT", ...Object.keys(rsBals)])).map((asset) => ({
      asset,
      balance: Number(rsBals[asset] || 0),
    }));

    openModal(
      "设置当前交易所真实精度残余",
      `
        <div class="alert-box">
          若你已交易过几轮、目前交易所里本身存有因最小交易单位留下的零头，可在此直接填入当前真实剩余（支持任意币种）。后续每笔流水或提现都会在这个底数上继续「+留余 / -消耗 / 清零」。
        </div>
        <div id="cal-dust-list" style="display:flex;flex-direction:column;gap:8px;"></div>
        <div>
          <button type="button" class="btn btn-sm btn-secondary" id="btn-cal-add-asset">+ 添加其他币种残余</button>
        </div>
      `,
      [
        { text: "取消", onClick: closeModal },
        {
          text: "保存当前残余",
          cls: "btn-primary",
          onClick: async () => {
            const payload = {};
            document.querySelectorAll("#cal-dust-list .cal-dust-row").forEach((rowEl) => {
              const code = rowEl.querySelector(".cal-asset")?.value?.trim().toUpperCase();
              const val = Number(rowEl.querySelector(".cal-val")?.value || 0);
              if (code && Number.isFinite(val) && val >= 0) {
                payload[code] = val;
              }
            });
            try {
              const res = await api().calibrate_residual_balance(payload, state.data?.active_wallet?.id || null);
              closeModal();
              applyStatePayload(res.state);
              toast("已更新交易所当前残余余额");
            } catch (e) {
              toast(`保存失败: ${e.message || e}`, "error");
            }
          },
        },
      ]
    );

    const renderCalRows = () => {
      const box = document.getElementById("cal-dust-list");
      box.innerHTML = rows
        .map(
          (r, idx) => `
          <div class="cal-dust-row" style="display:flex;gap:8px;align-items:center;">
            <input type="text" class="input mono-addr cal-asset" data-idx="${idx}" value="${escapeHtml(r.asset)}" placeholder="币种" style="width:90px;text-transform:uppercase;" />
            <input type="number" step="0.000001" min="0" class="input cal-val" data-idx="${idx}" value="${r.balance}" style="flex:1;" />
            <button type="button" class="btn btn-sm btn-secondary cal-zero" data-idx="${idx}">清零</button>
          </div>
        `
        )
        .join("");
      box.querySelectorAll(".cal-asset").forEach((el) => {
        el.oninput = () => { rows[Number(el.dataset.idx)].asset = el.value.trim().toUpperCase(); };
      });
      box.querySelectorAll(".cal-val").forEach((el) => {
        el.oninput = () => { rows[Number(el.dataset.idx)].balance = Number(el.value || 0); };
      });
      box.querySelectorAll(".cal-zero").forEach((btn) => {
        btn.onclick = () => {
          rows[Number(btn.dataset.idx)].balance = 0;
          renderCalRows();
        };
      });
    };
    renderCalRows();
    document.getElementById("btn-cal-add-asset").onclick = () => {
      rows.push({ asset: "EUR", balance: 0 });
      renderCalRows();
    };
  }

  function openFeePipelineEditorModal() {
    const presets = (state.data && state.data.fee_pipeline_presets) || [];
    let editSteps = ((state.data && state.data.fee_pipeline_templates) || []).map((s) => ({ ...s }));
    const presetOpts = presets
      .map((p) => `<option value="${escapeHtml(p.key)}">${escapeHtml(p.label)} — ${escapeHtml(p.description)}</option>`)
      .join("");

    openModal(
      "⚙️ 自定义提现 / 交易手续费流程模板",
      `
        <div class="alert-box">
          按你自己的真实出金路线自由定义默认手续费环节（保存后每次结算自动带出；若你的出金方式不同，可一键套用预设再增删改）。
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          <select id="fpe-preset-sel" class="input" style="flex:1;">${presetOpts}</select>
          <button type="button" class="btn btn-sm btn-secondary" id="fpe-apply-preset">套用预设</button>
          <button type="button" class="btn btn-sm btn-primary" id="fpe-add-step">+ 添加自定义环节</button>
        </div>
        <div id="fpe-steps-list" style="display:flex;flex-direction:column;gap:8px;max-height:360px;overflow-y:auto;"></div>
      `,
      [
        { text: "取消", onClick: closeModal },
        {
          text: "保存默认流程",
          cls: "btn-primary",
          onClick: async () => {
            const clean = editSteps.filter((s) => (s.name || "").trim());
            try {
              const prevAmounts = new Map((state.feeFormState?.fee_entries || []).map((x) => [x.step_id, x.amount]));
              const res = await api().save_fee_pipeline_templates(clean, state.data?.active_wallet?.id || null);
              state.feeFormState = makeDefaultFeeFormState();
              applyStatePayload(res.state);
              if (state.feeFormState) {
                state.feeFormState.fee_entries.forEach((e) => {
                  if (prevAmounts.has(e.step_id) && prevAmounts.get(e.step_id) > 0) e.amount = prevAmounts.get(e.step_id);
                });
              }
              renderFeeDynamicPanels();
              updateFeeLiveSummary();
              closeModal();
              toast(`已保存自定义手续费流程（共 ${res.templates.length} 个环节）`);
            } catch (e) {
              toast(`保存失败: ${e.message || e}`, "error");
            }
          },
        },
      ],
      true
    );

    const renderEditorRows = () => {
      const box = document.getElementById("fpe-steps-list");
      if (!editSteps.length) {
        box.innerHTML = `<div class="empty-state">暂无手续费环节，点击右上角「+ 添加自定义环节」添加</div>`;
        return;
      }
      box.innerHTML = editSteps
        .map((s, idx) => {
          const catOpts = FEE_CATEGORY_OPTS.map(
            ([k, lbl]) => `<option value="${k}" ${s.category === k ? "selected" : ""}>${lbl}</option>`
          ).join("");
          return `
            <div style="display:grid;grid-template-columns:1.3fr 0.85fr 72px 78px auto auto auto;gap:6px;align-items:center;padding:8px;border:1px solid var(--border);border-radius:7px;background:var(--bg-elevated);">
              <input type="text" class="input-sm fpe-name" data-idx="${idx}" value="${escapeHtml(s.name)}" placeholder="环节名称（如 交易所提现）" />
              <select class="input-sm fpe-cat" data-idx="${idx}">${catOpts}</select>
              <input type="text" class="input-sm fpe-cur mono-addr" data-idx="${idx}" value="${escapeHtml(s.fee_currency || "USDT")}" placeholder="币种" style="text-transform:uppercase;" />
              <input type="number" class="input-sm fpe-def" data-idx="${idx}" step="0.000001" min="0" value="${s.default_fee || 0}" title="默认预填费" />
              <label style="font-size:11px;display:flex;align-items:center;gap:3px;white-space:nowrap;" title="交易所直充钱包时自动免除">
                <input type="checkbox" class="fpe-off" data-idx="${idx}" ${s.only_official_wallet ? "checked" : ""} /> 仅官方
              </label>
              <label style="font-size:11px;display:flex;align-items:center;gap:3px;white-space:nowrap;">
                <input type="checkbox" class="fpe-en" data-idx="${idx}" ${s.enabled !== false ? "checked" : ""} /> 启用
              </label>
              <div style="display:flex;gap:3px;">
                <button type="button" class="link-btn fpe-up" data-idx="${idx}" title="上移">↑</button>
                <button type="button" class="link-btn danger fpe-del" data-idx="${idx}" title="删除">&times;</button>
              </div>
            </div>
          `;
        })
        .join("");

      box.querySelectorAll(".fpe-name").forEach((el) => { el.oninput = () => { editSteps[Number(el.dataset.idx)].name = el.value; }; });
      box.querySelectorAll(".fpe-cat").forEach((el) => { el.onchange = () => { editSteps[Number(el.dataset.idx)].category = el.value; }; });
      box.querySelectorAll(".fpe-cur").forEach((el) => { el.oninput = () => { editSteps[Number(el.dataset.idx)].fee_currency = el.value.trim().toUpperCase() || "USDT"; }; });
      box.querySelectorAll(".fpe-def").forEach((el) => { el.oninput = () => { editSteps[Number(el.dataset.idx)].default_fee = Math.max(0, Number(el.value || 0)); }; });
      box.querySelectorAll(".fpe-off").forEach((el) => { el.onchange = () => { editSteps[Number(el.dataset.idx)].only_official_wallet = el.checked; }; });
      box.querySelectorAll(".fpe-en").forEach((el) => { el.onchange = () => { editSteps[Number(el.dataset.idx)].enabled = el.checked; }; });
      box.querySelectorAll(".fpe-up").forEach((btn) => {
        btn.onclick = () => {
          const i = Number(btn.dataset.idx);
          if (i > 0) {
            const tmp = editSteps[i - 1];
            editSteps[i - 1] = editSteps[i];
            editSteps[i] = tmp;
            renderEditorRows();
          }
        };
      });
      box.querySelectorAll(".fpe-del").forEach((btn) => {
        btn.onclick = () => {
          editSteps.splice(Number(btn.dataset.idx), 1);
          renderEditorRows();
        };
      });
    };

    renderEditorRows();
    document.getElementById("fpe-add-step").onclick = () => {
      editSteps.push({
        id: `custom_${Date.now()}`,
        name: "自定义手续费环节",
        category: "other",
        fee_currency: "USDT",
        only_official_wallet: false,
        default_fee: 0,
        enabled: true,
        notes: "",
      });
      renderEditorRows();
    };
    document.getElementById("fpe-apply-preset").onclick = () => {
      const k = document.getElementById("fpe-preset-sel").value;
      const found = presets.find((p) => p.key === k);
      if (found && Array.isArray(found.steps)) {
        editSteps = found.steps.map((s) => ({ ...s }));
        renderEditorRows();
        toast(`已加载预设「${found.label}」，请点击「保存默认流程」生效`);
      }
    };
  }

  // -------------------------------------------------------------------
  // First-Run Onboarding Wizard
  // -------------------------------------------------------------------
  function maybeSyncOnboardingModal() {
    const ov = document.getElementById("onboarding-overlay");
    if (!ov || !state.data) return;
    if (!state.data.needs_onboarding) {
      state.onboardingSkipped = false;
      ov.classList.add("hidden");
      return;
    }
    if (!state.onboardingSkipped) {
      openOnboardingModal();
    }
  }

  function renderOnboardingMembers() {
    const box = document.getElementById("ob-members-list");
    if (!box) return;
    box.innerHTML = (state.onboardingMembers || [])
      .map(
        (m, idx) => `
        <div class="ob-member-row">
          <input type="text" class="input-sm ob-m-name" data-idx="${idx}" value="${escapeHtml(m.name)}" placeholder="成员名（如 Alice）" />
          <input type="text" class="input-sm ob-m-pfx" data-idx="${idx}" value="${escapeHtml(m.prefixes)}" placeholder="矿机前缀（如 alice, a01）" />
          <input type="color" class="ob-m-col" data-idx="${idx}" value="${escapeHtml(m.color || "#10b981")}" style="width:42px;height:30px;background:none;border:none;padding:0;" />
          <button type="button" class="link-btn danger ob-m-del" data-idx="${idx}">删除</button>
        </div>
      `
      )
      .join("");
    box.querySelectorAll(".ob-m-name").forEach((el) => { el.oninput = () => { state.onboardingMembers[Number(el.dataset.idx)].name = el.value; }; });
    box.querySelectorAll(".ob-m-pfx").forEach((el) => { el.oninput = () => { state.onboardingMembers[Number(el.dataset.idx)].prefixes = el.value; }; });
    box.querySelectorAll(".ob-m-col").forEach((el) => { el.oninput = () => { state.onboardingMembers[Number(el.dataset.idx)].color = el.value; }; });
    box.querySelectorAll(".ob-m-del").forEach((btn) => {
      btn.onclick = () => {
        state.onboardingMembers.splice(Number(btn.dataset.idx), 1);
        renderOnboardingMembers();
      };
    });
  }

  function openOnboardingModal() {
    const ov = document.getElementById("onboarding-overlay");
    if (!ov) return;
    if (!state.onboardingMembers || !state.onboardingMembers.length) {
      state.onboardingMembers = [
        { name: "", prefixes: "", color: "#10b981" },
        { name: "", prefixes: "", color: "#3b82f6" },
      ];
    }
    renderOnboardingMembers();
    ov.classList.remove("hidden");
  }

  // -------------------------------------------------------------------
  // Members & Prefixes View
  // -------------------------------------------------------------------
  function renderMembers() {
    const { members, global_totals } = state.data;
    const grid = document.getElementById("members-grid");
    grid.innerHTML = members
      .map((m) => {
        const prefixChips = (m.prefixes || [])
          .map(
            (p) =>
              `<span class="prefix-chip">${escapeHtml(p.prefix)}<button data-del-prefix="${p.id}" title="删除前缀">&times;</button></span>`
          )
          .join("");

        const bindingChips = (m.bindings || [])
          .map(
            (b) =>
              `<span class="prefix-chip" style="border-color:rgba(59,130,246,0.4)">矿机:${escapeHtml(b.worker_name)}<button data-del-binding="${b.id}" title="解绑">&times;</button></span>`
          )
          .join("");

        return `
          <div class="member-card" style="border-top: 3px solid ${escapeHtml(m.color)}">
            <div class="member-card-head">
              <div style="display:flex;align-items:center;gap:8px;">
                <span class="member-dot" style="background:${escapeHtml(m.color)};width:10px;height:10px;"></span>
                <strong style="font-size:15px;">${escapeHtml(m.name)}</strong>
              </div>
              <div>
                <button class="link-btn" data-add-prefix-member="${m.id}" data-member-name="${escapeHtml(m.name)}">+ 前缀</button>
                <button class="link-btn" data-edit-member="${m.id}">编辑</button>
                <button class="link-btn danger" data-del-member="${m.id}" data-member-name="${escapeHtml(m.name)}">删除</button>
              </div>
            </div>
            <div>
              <div style="font-size:11.5px;color:var(--text-muted);margin-bottom:5px;">自动识别前缀：</div>
              <div class="tag-list">${prefixChips || '<span style="color:var(--text-dim);font-size:12px;">暂无前缀</span>'}</div>
            </div>
            ${
              bindingChips
                ? `<div><div style="font-size:11.5px;color:var(--text-muted);margin-bottom:5px;">手动精确绑定矿机：</div><div class="tag-list">${bindingChips}</div></div>`
                : ""
            }
          </div>
        `;
      })
      .join("");

    grid.querySelectorAll("[data-del-prefix]").forEach((b) => {
      b.onclick = async () => {
        const next = await api().delete_prefix(Number(b.dataset.delPrefix));
        applyStatePayload(next);
        toast("已移除前缀");
      };
    });
    grid.querySelectorAll("[data-del-binding]").forEach((b) => {
      b.onclick = async () => {
        const next = await api().delete_worker_binding(Number(b.dataset.delBinding));
        applyStatePayload(next);
        toast("已解绑矿机");
      };
    });
    grid.querySelectorAll("[data-add-prefix-member]").forEach((b) => {
      b.onclick = () => openAddPrefixModal(Number(b.dataset.addPrefixMember), b.dataset.memberName);
    });
    grid.querySelectorAll("[data-edit-member]").forEach((b) => {
      b.onclick = () => {
        const m = members.find((x) => x.id === Number(b.dataset.editMember));
        if (m) openMemberModal(m);
      };
    });
    grid.querySelectorAll("[data-del-member]").forEach((b) => {
      b.onclick = async () => {
        const ok = await confirmDialog(
          `确定删除成员 "${b.dataset.memberName}" 及其前缀规则吗？（历史结算记录仍保留名称）`,
          { title: "删除成员", confirmText: "确认删除", danger: true }
        );
        if (!ok) return;
        const next = await api().delete_member(Number(b.dataset.delMember));
        applyStatePayload(next);
        toast("已删除成员");
      };
    });

    const gBody = document.getElementById("global-totals-tbody");
    if (!global_totals || !global_totals.length) {
      gBody.innerHTML = `<tr><td colspan="5" class="empty-state">暂无历史提现结算统计</td></tr>`;
    } else {
      gBody.innerHTML = global_totals
        .map(
          (g) => `
          <tr>
            <td>
              <span class="member-tag">
                <span class="member-dot" style="background:${escapeHtml(g.member_color)}"></span>
                ${escapeHtml(g.member_name)}
              </span>
            </td>
            <td class="num">${g.settlement_count} 次</td>
            <td class="num">${fmtInt(g.total_shares)}</td>
            <td class="num">${Number(g.total_prl || 0).toFixed(6)} PRL</td>
            <td class="num"><strong style="color:#34d399">￥${fmtMoney(g.total_cny)}</strong></td>
          </tr>
        `
        )
        .join("");
    }
  }

  // -------------------------------------------------------------------
  // Settings View
  // -------------------------------------------------------------------
  function renderSettings() {
    const { settings, data_dir, residual_summary } = state.data;
    document.getElementById("set-auto-poll").checked = settings.auto_poll_enabled === "1";
    document.getElementById("set-poll-interval").value = settings.auto_poll_interval_sec || "60";
    document.getElementById("set-minimize-tray").checked = settings.minimize_to_tray === "1";
    document.getElementById("settings-db-path").textContent = `${data_dir}\\pearl_split.db`;

    const rs = residual_summary || {};
    const rsBals = rs.balances || {};
    const prlIn = document.getElementById("set-dust-prl");
    const usdtIn = document.getElementById("set-dust-usdt");
    const ae = document.activeElement;
    if (prlIn && ae !== prlIn) {
      const pVal = Number(rsBals.PRL ?? rs.prl_dust_balance ?? 0);
      prlIn.value = pVal !== 0 ? String(pVal) : "0";
    }
    if (usdtIn && ae !== usdtIn) {
      const uVal = Number(rsBals.USDT ?? rs.usdt_dust_balance ?? 0);
      usdtIn.value = uVal !== 0 ? String(uVal) : "0";
    }
    const bdEl = document.getElementById("set-dust-breakdown");
    if (bdEl) {
      const parts = Object.entries(rsBals).map(([k, v]) => `${k} ${fmtPrl(v || 0)}`);
      bdEl.textContent = `当前残余池：${parts.join(" · ") || "PRL 0 · USDT 0"}`;
    }
  }

  // -------------------------------------------------------------------
  // Offline Alert Push, Remote Credentials & Synology Standby Failover
  // -------------------------------------------------------------------
  function syncAlertProviderBoxes() {
    const prov = document.getElementById("alert-provider")?.value || "pushplus";
    const ppBox = document.getElementById("alert-box-pushplus");
    const wxBox = document.getElementById("alert-box-wxpusher");
    if (ppBox) ppBox.classList.toggle("hidden", prov !== "pushplus");
    if (wxBox) wxBox.classList.toggle("hidden", prov !== "wxpusher");
  }

  function collectAlertConfigPayload() {
    return {
      enabled: Boolean(document.getElementById("alert-enabled")?.checked),
      provider: document.getElementById("alert-provider")?.value || "pushplus",
      pushplus_token: document.getElementById("alert-pp-token")?.value?.trim() || "",
      pushplus_topic: document.getElementById("alert-pp-topic")?.value?.trim() || "",
      pushplus_channel: "wechat",
      wxpusher_app_token: document.getElementById("alert-wx-token")?.value?.trim() || "",
      wxpusher_topic_ids: document.getElementById("alert-wx-topics")?.value?.trim() || "",
      wxpusher_uids: document.getElementById("alert-wx-uids")?.value?.trim() || "",
      offline_confirm_polls: Number(document.getElementById("alert-confirm-polls")?.value || 2),
      poll_fail_alert_minutes: Number(document.getElementById("alert-poll-fail-min")?.value || 30),
      notify_recovery: Boolean(document.getElementById("alert-notify-recovery")?.checked),
      include_password: Boolean(document.getElementById("alert-include-password")?.checked),
      standby_sync_enabled: Boolean(document.getElementById("alert-standby-enabled")?.checked),
      standby_server_url: document.getElementById("alert-standby-url")?.value?.trim() || "",
      standby_token: document.getElementById("alert-standby-token")?.value?.trim() || "",
      desktop_lease_timeout_sec: Number(document.getElementById("alert-lease-sec")?.value || 180),
      standby_poll_interval_sec: Number(document.getElementById("alert-standby-poll-sec")?.value || 60),
    };
  }

  function openWorkerRemoteModal(workerObj) {
    const wallet = state.data && state.data.active_wallet;
    if (!wallet || !workerObj) return;
    const tool = workerObj.remote_tool || "ToDesk";
    const opts = ["ToDesk", "UU远程", "向日葵", "RustDesk", "AnyDesk", "自定义"]
      .map((t) => `<option value="${t}" ${t === tool ? "selected" : ""}>${t}</option>`)
      .join("");
    openModal(
      `🔑 配置矿机远控凭证 · ${workerObj.worker_name}`,
      `
        <div class="alert-box">
          ToDesk / UU 远程本地配置文件中的密码均为加密存储，建议先在目标矿机设定<strong>固定安全密码</strong>后填入下方；当矿机掉线触发微信通知时，系统会把配对码和密码一并带入提醒。
        </div>
        <div class="fee-fields-grid">
          <div class="form-field">
            <label>远程桌面工具</label>
            <select id="wr-tool" class="input">${opts}</select>
          </div>
          <div class="form-field">
            <label>设备代码 / 配对码</label>
            <input id="wr-code" class="input mono-addr" value="${escapeHtml(workerObj.remote_code || "")}" placeholder="例如 123 456 789" />
          </div>
          <div class="form-field">
            <label>固定连接密码</label>
            <input id="wr-pwd" class="input mono-addr" value="${escapeHtml(workerObj.remote_password || "")}" placeholder="建议设为长期固定密码" />
          </div>
          <div class="form-field">
            <label>备注说明 (可选)</label>
            <input id="wr-note" class="input" value="${escapeHtml(workerObj.remote_note || "")}" placeholder="例如 放在卧室 / 机主昵称" />
          </div>
        </div>
        <label class="lm-inline-check" style="margin-top:4px;">
          <input type="checkbox" id="wr-alert-enabled" ${workerObj.alert_enabled !== 0 ? "checked" : ""} />
          <span>启用本台矿机掉线推送告警（出差主动长期关机时可取消勾选）</span>
        </label>
      `,
      [
        { text: "取消", onClick: closeModal },
        {
          text: "保存远控配置",
          cls: "btn-primary",
          onClick: async () => {
            try {
              const res = await api().update_worker_remote_config(wallet.id, workerObj.worker_name, {
                remote_tool: document.getElementById("wr-tool").value,
                remote_code: document.getElementById("wr-code").value,
                remote_password: document.getElementById("wr-pwd").value,
                remote_note: document.getElementById("wr-note").value,
                alert_enabled: document.getElementById("wr-alert-enabled").checked,
              });
              closeModal();
              applyStatePayload(res.state);
              toast(`已保存矿机 ${workerObj.worker_name} 的远控与告警设置`);
            } catch (e) {
              toast(`保存失败: ${e.message || e}`, "error");
            }
          },
        },
      ]
    );
  }

  function renderAlertsView(opts = {}) {
    if (!state.data) return;
    const cfg = state.data.alert_config || {};
    const pol = state.data.poller || {};
    const workers = state.data.workers || [];
    const logs = state.data.alert_logs || [];
    const wallet = state.data.active_wallet;

    const ae = document.activeElement;
    const editingAlertCfg =
      !opts.force &&
      ae &&
      ae.closest &&
      ae.closest("#view-alerts") &&
      !ae.closest("#alert-workers-tbody");

    const masterBadge = document.getElementById("alert-master-badge");
    if (masterBadge) {
      if (cfg.enabled) {
        const pname = cfg.provider === "wxpusher" ? "WxPusher" : "PushPlus";
        masterBadge.className = "status-badge status-online";
        masterBadge.textContent = `已启用 · ${pname}`;
      } else {
        masterBadge.className = "status-badge status-manual";
        masterBadge.textContent = "未开启自动告警";
      }
    }

    if (!editingAlertCfg) {
      const setVal = (id, v) => {
        const el = document.getElementById(id);
        if (el) el.value = v ?? "";
      };
      const setChk = (id, b) => {
        const el = document.getElementById(id);
        if (el) el.checked = Boolean(b);
      };
      setChk("alert-enabled", cfg.enabled);
      setVal("alert-provider", cfg.provider || "pushplus");
      setVal("alert-pp-token", cfg.pushplus_token || "");
      setVal("alert-pp-topic", cfg.pushplus_topic || "");
      setVal("alert-wx-token", cfg.wxpusher_app_token || "");
      setVal("alert-wx-topics", cfg.wxpusher_topic_ids || "");
      setVal("alert-wx-uids", cfg.wxpusher_uids || "");
      setVal("alert-confirm-polls", String(cfg.offline_confirm_polls || 2));
      setVal("alert-poll-fail-min", String(cfg.poll_fail_alert_minutes || 30));
      setChk("alert-notify-recovery", cfg.notify_recovery !== false);
      setChk("alert-include-password", cfg.include_password !== false);
      setChk("alert-standby-enabled", Boolean(cfg.standby_sync_enabled));
      setVal("alert-standby-url", cfg.standby_server_url || "");
      setVal("alert-standby-token", cfg.standby_token || "");
      setVal("alert-lease-sec", String(cfg.desktop_lease_timeout_sec || 180));
      setVal("alert-standby-poll-sec", String(cfg.standby_poll_interval_sec || 60));
      syncAlertProviderBoxes();
    }

    // Standby status badge & diagnostic box
    const sbBadge = document.getElementById("alert-standby-badge");
    const sbBox = document.getElementById("alert-standby-status-box");
    const remote = pol.standby_remote_status;
    if (sbBadge && sbBox) {
      if (!cfg.standby_sync_enabled) {
        sbBadge.className = "status-badge status-manual";
        sbBadge.textContent = "未开启备援联动";
        sbBox.innerHTML =
          cfg.effective_standby_url
            ? `检测到你已配置远程集群地址 <code>${escapeHtml(cfg.effective_standby_url)}</code>，勾选「开启主备联动」并保存配置即可在关机后由群晖自动接管告警。`
            : "开启并填入群晖 Docker 后端地址后，当电脑关闭或断网超过租约时间，群晖 Docker 将自动接管矿池轮询与微信推送。";
      } else if (pol.standby_last_error) {
        sbBadge.className = "status-badge status-offline";
        sbBadge.textContent = "群晖连接异常";
        sbBox.innerHTML = `<span style="color:#fca5a5;">⚠️ 最近同步失败 (${escapeHtml(pol.standby_last_error)})</span> · 请确认群晖容器运行中且已升级包含 <code>alert_engine.py</code>。`;
      } else if (remote) {
        if (remote.mode === "standby_active") {
          sbBadge.className = "status-badge status-manual";
          sbBadge.textContent = "群晖曾处于接管模式";
        } else {
          sbBadge.className = "status-badge status-online";
          sbBadge.textContent = "🟢 桌面主控中 · 群晖热备待命";
        }
        sbBox.innerHTML =
          `✅ 最近租约同步：<strong>${escapeHtml(pol.standby_last_sync_at || "--")}</strong> ｜ ` +
          `群晖托管钱包：<strong>${remote.tracked_wallets ?? 0}</strong> 个 · 矿机：<strong>${remote.tracked_workers ?? 0}</strong> 台 ｜ ` +
          `租约超时接管阈值：${remote.lease_timeout_sec ?? cfg.desktop_lease_timeout_sec} 秒` +
          (remote.last_poll_at ? ` ｜ 群晖最近一次接管巡检：${escapeHtml(remote.last_poll_at)}` : "");
      } else {
        sbBadge.className = "status-badge status-manual";
        sbBadge.textContent = "待首次同步租约";
        sbBox.textContent = "配置已开启，请点击「立即同步并检查群晖」或等待下一次自动拉取完成首次握手。";
      }
    }

    // Per-worker remote credentials table (skip re-rendering while user is actively editing row inputs)
    const editingWorkerTable = !opts.force && ae && ae.closest && ae.closest("#alert-workers-tbody");
    const wBody = document.getElementById("alert-workers-tbody");
    if (wBody && !editingWorkerTable) {
      if (!workers.length) {
        wBody.innerHTML = `<tr><td colspan="8" class="empty-state">暂无缓存矿机，请先点击顶部「立即拉取」同步矿机列表</td></tr>`;
      } else {
        const toolChoices = ["ToDesk", "UU远程", "向日葵", "RustDesk", "AnyDesk", "自定义"];
        wBody.innerHTML = workers
          .map((w) => {
            const onStr = w.is_currently_online
              ? `<span class="status-badge status-online">● 在线</span>`
              : `<span class="status-badge status-offline">◔ 离线</span>`;
            const muted = Number(w.alert_enabled ?? 1) === 0;
            const sent = Number(w.offline_alert_sent || 0) === 1;
            const streak = Number(w.consecutive_offline_polls || 0);
            let lockBadge = `<span class="status-badge status-online" style="font-size:10.5px;">监听中</span>`;
            if (muted) {
              lockBadge = `<span class="status-badge status-manual" style="font-size:10.5px;">已静音免打扰</span>`;
            } else if (sent) {
              lockBadge = `<span class="status-badge status-offline" style="font-size:10.5px;" title="已发出掉线通知，上线前不再重复提醒">已告警·待上线重置</span>`;
            } else if (!w.is_currently_online && streak > 0) {
              lockBadge = `<span class="status-badge status-manual" style="font-size:10.5px;">离线确认 ${streak}/${cfg.offline_confirm_polls || 2}</span>`;
            }

            const curTool = w.remote_tool || "ToDesk";
            const optsHtml = toolChoices
              .map((t) => `<option value="${t}" ${t === curTool ? "selected" : ""}>${t}</option>`)
              .join("");

            return `
              <tr data-worker-name="${escapeHtml(w.worker_name)}">
                <td>
                  <strong>${escapeHtml(w.worker_name)}</strong>
                  <div style="font-size:11.5px;color:var(--text-muted);margin-top:2px;">
                    <span class="member-dot" style="background:${escapeHtml(w.member_color)}"></span>
                    ${escapeHtml(w.member_name)}
                  </div>
                </td>
                <td>
                  <div style="display:flex;gap:4px;flex-wrap:wrap;align-items:center;">${onStr}${lockBadge}</div>
                  ${w.last_alert_at ? `<div style="font-size:11px;color:var(--text-dim);margin-top:2px;">上次告警: ${escapeHtml(w.last_alert_at)}</div>` : ""}
                </td>
                <td><select class="input alert-worker-input aw-tool" style="min-width:92px;">${optsHtml}</select></td>
                <td><input type="text" class="input alert-worker-input mono-addr aw-code" value="${escapeHtml(w.remote_code || "")}" placeholder="设备配对码" /></td>
                <td><input type="password" onfocus="this.type='text'" onblur="this.type='password'" class="input alert-worker-input mono-addr aw-pwd" value="${escapeHtml(w.remote_password || "")}" placeholder="点击填写固定密码" /></td>
                <td><input type="text" class="input alert-worker-input aw-note" value="${escapeHtml(w.remote_note || "")}" placeholder="备注/机主" /></td>
                <td style="text-align:center;">
                  <input type="checkbox" class="aw-enabled" ${!muted ? "checked" : ""} title="取消勾选可关闭此机器掉线推送" />
                </td>
                <td class="act">
                  <button type="button" class="link-btn aw-save-btn">保存</button>
                  ${
                    sent
                      ? `<button type="button" class="link-btn aw-reset-btn" title="清除已告警锁并重新按当前状态判定">重置</button>`
                      : ""
                  }
                </td>
              </tr>
            `;
          })
          .join("");

        wBody.querySelectorAll(".aw-save-btn").forEach((btn) => {
          btn.onclick = async () => {
            if (!wallet) return;
            const tr = btn.closest("tr");
            const wname = tr?.dataset?.workerName;
            if (!tr || !wname) return;
            try {
              const res = await api().update_worker_remote_config(wallet.id, wname, {
                remote_tool: tr.querySelector(".aw-tool").value,
                remote_code: tr.querySelector(".aw-code").value,
                remote_password: tr.querySelector(".aw-pwd").value,
                remote_note: tr.querySelector(".aw-note").value,
                alert_enabled: tr.querySelector(".aw-enabled").checked,
              });
              applyStatePayload(res.state);
              toast(`已保存 ${wname} 的远控凭证与告警开关`);
            } catch (e) {
              toast(`保存失败: ${e.message || e}`, "error");
            }
          };
        });

        wBody.querySelectorAll(".aw-reset-btn").forEach((btn) => {
          btn.onclick = async () => {
            if (!wallet) return;
            const tr = btn.closest("tr");
            const wname = tr?.dataset?.workerName;
            if (!wname) return;
            try {
              const res = await api().reset_worker_alert_state(wallet.id, wname);
              applyStatePayload(res.state);
              toast(`已重置 ${wname} 的掉线告警锁定状态`);
            } catch (e) {
              toast(`重置失败: ${e.message || e}`, "error");
            }
          };
        });
      }
    }

    // Alert logs table
    const lBody = document.getElementById("alert-logs-tbody");
    if (lBody) {
      if (!logs.length) {
        lBody.innerHTML = `<tr><td colspan="6" class="empty-state">暂无告警推送历史</td></tr>`;
      } else {
        const evtLabelMap = {
          offline: `<span class="status-badge status-offline">掉线告警</span>`,
          recovery: `<span class="status-badge status-online">恢复上线</span>`,
          poll_fail: `<span class="status-badge status-manual">轮询超时</span>`,
          test: `<span class="status-badge" style="background:rgba(59,130,246,0.16);color:#93c5fd;">测试推送</span>`,
        };
        lBody.innerHTML = logs
          .map((lg) => {
            const srcBadge =
              lg.source === "docker"
                ? `<span class="status-badge status-manual">群晖Docker</span>`
                : `<span class="status-badge status-online">桌面端</span>`;
            const stBadge = lg.ok
              ? `<span style="color:#34d399;font-weight:600;">✓ 成功</span>`
              : `<span style="color:#f87171;" title="${escapeHtml(lg.error || "")}">✗ 失败: ${escapeHtml((lg.error || "").slice(0, 32))}</span>`;
            return `
              <tr>
                <td>${escapeHtml(lg.created_at)}</td>
                <td>${srcBadge}</td>
                <td>${evtLabelMap[lg.event_type] || escapeHtml(lg.event_type)}</td>
                <td><strong>${escapeHtml(lg.worker_names || lg.wallet_alias || "--")}</strong></td>
                <td title="${escapeHtml(lg.title || "")}">${escapeHtml(lg.summary || lg.title || "")}</td>
                <td>${stBadge}</td>
              </tr>
            `;
          })
          .join("");
      }
    }
  }

  // -------------------------------------------------------------------
  // Modals
  // -------------------------------------------------------------------
  function openAddWalletModal() {
    openModal(
      "添加新钱包地址",
      `
        <div class="form-field">
          <label>奈特矿池 Dashboard 链接或 PRL 钱包地址 <span class="req">*</span></label>
          <input id="m-wallet-input" class="input" placeholder="粘贴 https://www.ntminerpool.com/prl/index?back=prl1... 或 prl1..." />
        </div>
        <div class="form-field">
          <label>钱包别名（方便切换识别）</label>
          <input id="m-wallet-alias" class="input" placeholder="例如：宿舍主钱包 / 矿场钱包B" />
        </div>
        <div class="form-field">
          <label>钱包类别 <span class="req">*</span></label>
          <select id="m-wallet-type" class="input">
            <option value="official">Pearl 官方钱包（需先转账到交易所，支持记录转出手续费）</option>
            <option value="exchange">交易所钱包（矿池直充交易所，自动免除钱包转交易所步骤）</option>
          </select>
        </div>
        <div class="form-field">
          <label>备注 (可选)</label>
          <input id="m-wallet-notes" class="input" placeholder="用途或出金路线说明" />
        </div>
      `,
      [
        { text: "取消", onClick: closeModal },
        {
          text: "添加并拉取数据",
          cls: "btn-primary",
          onClick: async () => {
            try {
              const addr = document.getElementById("m-wallet-input").value;
              const alias = document.getElementById("m-wallet-alias").value;
              const notes = document.getElementById("m-wallet-notes").value;
              const wtype = document.getElementById("m-wallet-type").value;
              const next = await api().add_wallet(addr, alias, notes, wtype);
              state.settleDraft = null;
              state.settlePrevCheckpointId = null;
              closeModal();
              applyStatePayload(next);
              toast("已添加钱包并完成首次拉取");
            } catch (e) {
              toast(String(e), "error");
            }
          },
        },
      ]
    );
  }

  function openEditWalletModal(wallet) {
    openModal(
      "编辑当前钱包",
      `
        <div class="form-field">
          <label>钱包地址</label>
          <input class="input" value="${escapeHtml(wallet.address)}" disabled />
        </div>
        <div class="form-field">
          <label>钱包别名 <span class="req">*</span></label>
          <input id="m-ew-alias" class="input" value="${escapeHtml(wallet.alias)}" />
        </div>
        <div class="form-field">
          <label>钱包类别</label>
          <select id="m-ew-type" class="input">
            <option value="official" ${wallet.wallet_type !== "exchange" ? "selected" : ""}>Pearl 官方钱包（支持记录转交易所转账费）</option>
            <option value="exchange" ${wallet.wallet_type === "exchange" ? "selected" : ""}>交易所钱包（自动免除仅官方钱包转账费）</option>
          </select>
        </div>
        <div class="form-field">
          <label>备注</label>
          <input id="m-ew-notes" class="input" value="${escapeHtml(wallet.notes || "")}" />
        </div>
        <div class="setting-row" style="margin-top:4px;">
          <span class="setting-desc">参与后台自动挂机轮询（停用老钱包可减少无用请求）</span>
          <input type="checkbox" id="m-ew-active" ${wallet.is_active ? "checked" : ""} />
        </div>
      `,
      [
        {
          text: "删除此钱包",
          cls: "btn-danger",
          onClick: async () => {
            const ok = await confirmDialog(`确定删除钱包 "${wallet.alias}" 及其所有本地记录吗？`, {
              title: "删除钱包",
              confirmText: "确认删除",
              danger: true,
            });
            if (!ok) return;
            try {
              const next = await api().delete_wallet(wallet.id);
              state.settleDraft = null;
              state.settlePrevCheckpointId = null;
              closeModal();
              applyStatePayload(next);
              toast("钱包已删除");
            } catch (e) {
              toast(String(e), "error");
            }
          },
        },
        { text: "取消", onClick: closeModal },
        {
          text: "保存修改",
          cls: "btn-primary",
          onClick: async () => {
            try {
              const alias = document.getElementById("m-ew-alias").value;
              const notes = document.getElementById("m-ew-notes").value;
              const active = document.getElementById("m-ew-active").checked;
              const wtype = document.getElementById("m-ew-type").value;
              const next = await api().update_wallet(wallet.id, alias, notes, active, wtype);
              closeModal();
              applyStatePayload(next);
              toast("钱包设置已保存");
            } catch (e) {
              toast(String(e), "error");
            }
          },
        },
      ]
    );
  }

  function openQuickAssignModal(workerName, onDone = null) {
    const members = state.data.members || [];
    const suggested = (state.data.unassigned_workers || []).find((u) => u.worker_name === workerName)?.suggested_prefix || workerName.slice(0, 4);
    const memberOpts = members.map((m) => `<option value="${m.id}">${escapeHtml(m.name)}</option>`).join("");

    openModal(
      `分配矿机归属：${workerName}`,
      `
        <div class="form-field">
          <label>分配方式</label>
          <select id="qa-mode" class="input">
            <option value="add_prefix">将前缀添加到已有成员（以后同前缀自动识别）</option>
            <option value="bind_worker">仅手动绑定该矿机名到已有成员</option>
            <option value="new_member_prefix">新建成员并绑定前缀（以后自动识别）</option>
            <option value="new_member_worker">新建成员并仅绑定此矿机名</option>
          </select>
        </div>
        <div class="form-field" id="qa-existing-wrap">
          <label>选择已有成员</label>
          <select id="qa-member-id" class="input">${memberOpts}</select>
        </div>
        <div class="form-field hidden" id="qa-new-wrap">
          <label>新成员名称</label>
          <input id="qa-new-name" class="input" placeholder="例如：小王" />
          <label style="margin-top:8px;">代表颜色</label>
          <input id="qa-new-color" type="color" value="#06b6d4" style="width:60px;height:32px;background:none;border:none;" />
        </div>
        <div class="form-field" id="qa-prefix-wrap">
          <label>自动识别前缀（不区分大小写）</label>
          <input id="qa-prefix" class="input" value="${escapeHtml(suggested)}" />
        </div>
      `,
      [
        { text: "取消", onClick: closeModal },
        {
          text: "确定分配",
          cls: "btn-primary",
          onClick: async () => {
            try {
              const mode = document.getElementById("qa-mode").value;
              const mid = document.getElementById("qa-member-id").value;
              const newName = document.getElementById("qa-new-name").value;
              const newColor = document.getElementById("qa-new-color").value;
              const prefix = document.getElementById("qa-prefix").value;
              const next = await api().quick_assign_worker(
                workerName,
                mode,
                mid ? Number(mid) : null,
                newName,
                newColor,
                prefix,
                state.data.active_wallet.id
              );
              closeModal();
              applyStatePayload(next);
              if (onDone) await onDone();
              toast(`已成功归属矿机 ${workerName}`);
            } catch (e) {
              toast(String(e), "error");
            }
          },
        },
      ]
    );

    const modeEl = document.getElementById("qa-mode");
    const syncFields = () => {
      const m = modeEl.value;
      document.getElementById("qa-existing-wrap").classList.toggle("hidden", m.startsWith("new_member"));
      document.getElementById("qa-new-wrap").classList.toggle("hidden", !m.startsWith("new_member"));
      document.getElementById("qa-prefix-wrap").classList.toggle("hidden", m.endsWith("worker"));
    };
    modeEl.onchange = syncFields;
    syncFields();
  }

  function openManualWorkerModal(defaultName = "", defaultShares = 0) {
    const isEdit = Boolean(defaultName);
    openModal(
      isEdit ? `修正矿机缓存份额：${defaultName}` : "手动录入离线矿机份额",
      `
        <div class="form-field">
          <label>矿机名称 <span class="req">*</span></label>
          <input id="mw-name" class="input" value="${escapeHtml(defaultName)}" ${isEdit ? "disabled" : ""} placeholder="例如：bob_rig1" />
        </div>
        <div class="form-field">
          <label>当前累计有效份额 <span class="req">*</span></label>
          <input id="mw-shares" type="number" min="0" class="input" value="${defaultShares}" />
        </div>
        <p class="panel-desc">说明：用于矿池离线页为空时手动补充或修正矿机份额；该矿机下次上线后会被实时数据自动覆盖。</p>
      `,
      [
        { text: "取消", onClick: closeModal },
        {
          text: "保存份额",
          cls: "btn-primary",
          onClick: async () => {
            try {
              const name = document.getElementById("mw-name").value;
              const shares = Number(document.getElementById("mw-shares").value || 0);
              const next = await api().upsert_manual_worker(state.data.active_wallet.id, name, shares);
              closeModal();
              applyStatePayload(next);
              if (state.currentView === "settle") await initSettleDraft();
              toast("已更新离线矿机份额缓存");
            } catch (e) {
              toast(String(e), "error");
            }
          },
        },
      ]
    );
  }

  function openMemberModal(member = null) {
    const isEdit = Boolean(member);
    openModal(
      isEdit ? `编辑成员：${member.name}` : "添加新成员",
      `
        <div class="form-field">
          <label>成员名称 <span class="req">*</span></label>
          <input id="mm-name" class="input" value="${escapeHtml(member ? member.name : "")}" placeholder="例如：小李" />
        </div>
        <div class="form-field">
          <label>颜色标识</label>
          <input id="mm-color" type="color" value="${escapeHtml(member ? member.color : "#06b6d4")}" style="width:68px;height:34px;background:none;border:none;" />
        </div>
        ${
          !isEdit
            ? `<div class="form-field">
                <label>初始矿机前缀（多个可用逗号分隔，可留空）</label>
                <input id="mm-prefixes" class="input" placeholder="例如：xl, xiaoli" />
              </div>`
            : ""
        }
      `,
      [
        { text: "取消", onClick: closeModal },
        {
          text: "保存",
          cls: "btn-primary",
          onClick: async () => {
            try {
              const name = document.getElementById("mm-name").value;
              const color = document.getElementById("mm-color").value;
              let next;
              if (isEdit) {
                next = await api().update_member(member.id, name, color, "");
              } else {
                const rawP = document.getElementById("mm-prefixes").value || "";
                const prefixes = rawP.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
                next = await api().create_member(name, color, prefixes, "");
              }
              closeModal();
              applyStatePayload(next);
              toast("成员信息已保存");
            } catch (e) {
              toast(String(e), "error");
            }
          },
        },
      ]
    );
  }

  function openAddPrefixModal(memberId, memberName) {
    openModal(
      `为「${memberName}」添加自动识别前缀`,
      `
        <div class="form-field">
          <label>矿机名前缀（不区分大小写）</label>
          <input id="ap-prefix" class="input" placeholder="例如：bob2" />
        </div>
      `,
      [
        { text: "取消", onClick: closeModal },
        {
          text: "添加前缀",
          cls: "btn-primary",
          onClick: async () => {
            try {
              const p = document.getElementById("ap-prefix").value;
              const next = await api().add_prefix(memberId, p);
              closeModal();
              applyStatePayload(next);
              toast("已添加前缀规则");
            } catch (e) {
              toast(String(e), "error");
            }
          },
        },
      ]
    );
  }

  function openEditCheckpointModal(cp) {
    openModal(
      `修改提现到账金额 (${cp.checkpoint_time})`,
      `
        <div class="form-field">
          <label>实际到账人民币 (CNY) <span class="req">*</span></label>
          <input id="ecp-cny" type="number" step="0.01" min="0" class="input" value="${cp.cny_amount}" />
        </div>
        <div class="form-field">
          <label>提现 PRL 数量</label>
          <input id="ecp-prl" type="number" step="0.000001" min="0" class="input" value="${cp.prl_amount}" />
        </div>
        <div class="form-field">
          <label>标签</label>
          <input id="ecp-label" class="input" value="${escapeHtml(cp.label || "")}" />
        </div>
        <div class="form-field">
          <label>备注</label>
          <input id="ecp-notes" class="input" value="${escapeHtml(cp.notes || "")}" />
        </div>
        <p class="panel-desc">修改实际到账人民币后，系统会按该笔结算已锁定的增量份额比例自动重新分摊每位成员应得金额。</p>
      `,
      [
        { text: "取消", onClick: closeModal },
        {
          text: "保存并重算",
          cls: "btn-primary",
          onClick: async () => {
            try {
              const res = await api().update_settlement_amounts(
                cp.id,
                Number(document.getElementById("ecp-cny").value || 0),
                Number(document.getElementById("ecp-prl").value || 0),
                document.getElementById("ecp-label").value,
                cp.tx_hash || "",
                document.getElementById("ecp-notes").value
              );
              closeModal();
              applyStatePayload(res.state);
              toast("已更新金额并重新分账");
            } catch (e) {
              toast(String(e), "error");
            }
          },
        },
      ]
    );
  }

  function openSyncRecordsModal(defaultMode = "export", presetCheckpointIds = null) {
    const wallet = state.data && state.data.active_wallet;
    const cps = (state.data && state.data.checkpoints) || [];
    let activeTab = defaultMode === "import" ? "import" : "export";
    let scope = Array.isArray(presetCheckpointIds) && presetCheckpointIds.length ? "preset" : "all";
    let currentBundleRes = null;

    openModal(
      "🤝 提现记录与基准同步（发给朋友 / 导入同步）",
      `
        <div class="alert-box" style="margin-bottom:8px;">
          提现结算后，导出同步码或 <code>.json</code> 文件发给朋友；朋友导入后即可同步你的<strong>最新提现记录、各矿机锁定的份额起点和矿池已付快照</strong>，从而在自己电脑上实时看到从上次提现后新增的个人份额与待发收益。
        </div>
        <div style="display:flex;gap:8px;margin-bottom:8px;">
          <button type="button" class="btn btn-sm ${activeTab === "export" ? "btn-primary" : "btn-secondary"}" id="sync-tab-export">📤 导出发给朋友</button>
          <button type="button" class="btn btn-sm ${activeTab === "import" ? "btn-primary" : "btn-secondary"}" id="sync-tab-import">📥 导入朋友发来的记录</button>
        </div>

        <div id="sync-pane-export" class="${activeTab === "export" ? "" : "hidden"}" style="display:flex;flex-direction:column;gap:10px;">
          <div class="form-field">
            <label>导出范围（钱包：${escapeHtml(wallet ? wallet.alias : "无")})</label>
            <select id="sync-export-scope" class="input">
              ${
                Array.isArray(presetCheckpointIds) && presetCheckpointIds.length
                  ? `<option value="preset">仅导出当前选中的 ${presetCheckpointIds.length} 条记录（快速增量）</option>`
                  : ""
              }
              <option value="all">导出本钱包全部提现/基准历史（共 ${cps.length} 条 · 推荐）</option>
              ${cps.length > 0 ? `<option value="latest">仅导出最新 1 条起点记录 (${escapeHtml(cps[0].checkpoint_time)})</option>` : ""}
            </select>
          </div>
          <div id="sync-export-summary" class="setting-desc">正在打包同步数据...</div>
          <div style="display:flex;gap:10px;flex-wrap:wrap;">
            <button type="button" class="btn btn-primary" id="btn-sync-copy-code">📋 一键复制同步码（微信直发）</button>
            <button type="button" class="btn btn-secondary" id="btn-sync-save-file">💾 导出为同步文件 (.json)</button>
          </div>
          <div class="form-field">
            <label>同步码预览（<code>PEARLSYNC1:...</code> 可全选复制发给朋友）</label>
            <textarea id="sync-export-code-box" class="input" rows="4" readonly style="font-family:Consolas,monospace;font-size:11.5px;word-break:break-all;"></textarea>
          </div>
        </div>

        <div id="sync-pane-import" class="${activeTab === "import" ? "" : "hidden"}" style="display:flex;flex-direction:column;gap:10px;">
          <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
            <button type="button" class="btn btn-secondary btn-sm" id="btn-sync-pick-file">📂 从 .json 同步文件导入</button>
            <button type="button" class="btn btn-secondary btn-sm" id="btn-sync-paste-clip">📋 从剪切板读取同步码</button>
            <span class="setting-desc">支持粘贴 <code>PEARLSYNC1:...</code> 同步码或 JSON 文件全文</span>
          </div>
          <div class="form-field">
            <label>同步码 / JSON 数据内容</label>
            <textarea id="sync-import-text" class="input" rows="5" placeholder="将朋友发来的 PEARLSYNC1:... 同步码或 JSON 文本粘贴到这里，或点击上方「从 .json 同步文件导入」..."></textarea>
          </div>
          <div id="sync-import-preview" class="alert-box hidden"></div>
          <label class="lm-inline-check" style="margin-top:2px;">
            <input type="checkbox" id="sync-opt-members" checked />
            <span>同时合并钱包的成员标签、颜色与矿机前缀识别规则（不会覆盖你本机的矿机路径/系统设置）</span>
          </label>
          <div style="display:flex;justify-content:flex-end;">
            <button type="button" class="btn btn-primary" id="btn-sync-confirm-import">✅ 确认导入并更新份额收益</button>
          </div>
        </div>
      `,
      [{ text: "关闭", onClick: closeModal }],
      true
    );

    const switchSyncTab = (tab) => {
      activeTab = tab;
      document.getElementById("sync-pane-export").classList.toggle("hidden", tab !== "export");
      document.getElementById("sync-pane-import").classList.toggle("hidden", tab !== "import");
      document.getElementById("sync-tab-export").className =
        `btn btn-sm ${tab === "export" ? "btn-primary" : "btn-secondary"}`;
      document.getElementById("sync-tab-import").className =
        `btn btn-sm ${tab === "import" ? "btn-primary" : "btn-secondary"}`;
    };
    document.getElementById("sync-tab-export").onclick = () => switchSyncTab("export");
    document.getElementById("sync-tab-import").onclick = () => switchSyncTab("import");

    const getScopedIds = () => {
      const sel = document.getElementById("sync-export-scope")?.value || scope;
      if (sel === "preset" && Array.isArray(presetCheckpointIds) && presetCheckpointIds.length) {
        return presetCheckpointIds;
      }
      if (sel === "latest" && cps.length > 0) {
        return [cps[0].id];
      }
      return null;
    };

    const refreshExportCode = async () => {
      const sumEl = document.getElementById("sync-export-summary");
      const box = document.getElementById("sync-export-code-box");
      if (!wallet) {
        if (sumEl) sumEl.textContent = "请先在左侧选择或添加一个钱包。";
        if (box) box.value = "";
        return;
      }
      try {
        currentBundleRes = await api().get_wallet_sync_bundle(wallet.id, getScopedIds());
        if (box) box.value = currentBundleRes.sync_code || "";
        if (sumEl) {
          sumEl.innerHTML =
            `已就绪：含 <strong>${currentBundleRes.withdrawal_count}</strong> 条提现结算 + <strong>${currentBundleRes.baseline_count}</strong> 条基准快照` +
            `（同步码长度 ${(currentBundleRes.sync_code || "").length} 字符）`;
        }
      } catch (e) {
        if (sumEl) sumEl.textContent = `生成同步包失败: ${e}`;
      }
    };

    const scopeSel = document.getElementById("sync-export-scope");
    if (scopeSel) scopeSel.onchange = refreshExportCode;
    refreshExportCode();

    document.getElementById("btn-sync-copy-code").onclick = async () => {
      if (!currentBundleRes || !currentBundleRes.sync_code) {
        await refreshExportCode();
      }
      const code = currentBundleRes && currentBundleRes.sync_code;
      if (!code) return toast("暂无可复制的同步码", "warn");
      await copyText(code, "已复制提现同步码！发送给好友粘贴导入即可同步最新份额起点");
    };

    document.getElementById("btn-sync-save-file").onclick = async () => {
      if (!wallet) return toast("请先选择钱包", "warn");
      try {
        const res = await api().export_sync_file(wallet.id, getScopedIds(), null);
        if (!res.cancelled) {
          toast(`已导出同步文件 (${res.checkpoint_count} 条记录): ${res.path}`);
        }
      } catch (e) {
        toast(`导出同步文件失败: ${e}`, "error");
      }
    };

    const updateImportPreview = async () => {
      const txt = document.getElementById("sync-import-text").value.trim();
      const prevEl = document.getElementById("sync-import-preview");
      if (!txt) {
        prevEl.classList.add("hidden");
        prevEl.innerHTML = "";
        return;
      }
      try {
        const p = await api().preview_sync_import(txt);
        prevEl.classList.remove("hidden");
        const targetDesc = p.existing_wallet_alias
          ? `将合并到本机已有钱包「<strong>${escapeHtml(p.existing_wallet_alias)}</strong>」`
          : `本机尚无此地址，将自动新增钱包「<strong>${escapeHtml(p.wallet_alias)}</strong>」`;
        prevEl.innerHTML =
          `✅ 识别成功：${targetDesc}（<code>${escapeHtml(p.wallet_address.slice(0, 12))}...${escapeHtml(p.wallet_address.slice(-6))}</code>）<br/>` +
          `包含 <strong>${p.withdrawal_count}</strong> 条提现结算 + <strong>${p.baseline_count}</strong> 条基准快照 + ${p.member_count} 名成员规则` +
          (p.latest_time ? ` · 最新节点 ${escapeHtml(p.latest_time)}` : "");
      } catch (e) {
        prevEl.classList.remove("hidden");
        prevEl.innerHTML = `<span style="color:#fca5a5;">⚠️ ${escapeHtml(String(e.message || e))}</span>`;
      }
    };

    document.getElementById("sync-import-text").addEventListener("input", updateImportPreview);

    document.getElementById("btn-sync-pick-file").onclick = async () => {
      try {
        const res = await api().open_and_preview_sync_file(null);
        if (res.cancelled) return;
        document.getElementById("sync-import-text").value = res.raw_text || "";
        await updateImportPreview();
        toast(`已读取同步文件：${res.file_path}`);
      } catch (e) {
        toast(`读取同步文件失败: ${e.message || e}`, "error");
      }
    };

    document.getElementById("btn-sync-paste-clip").onclick = async () => {
      const inp = document.getElementById("sync-import-text");
      try {
        if (navigator.clipboard && navigator.clipboard.readText) {
          const clip = await navigator.clipboard.readText();
          if (clip && clip.trim()) {
            inp.value = clip.trim();
            await updateImportPreview();
            return;
          }
        }
      } catch {
        // Clipboard permission denied in WebView -> fall back to manual Ctrl+V
      }
      inp.focus();
      toast("请按 Ctrl+V 将同步码粘贴到下方输入框中", "warn");
    };

    document.getElementById("btn-sync-confirm-import").onclick = async () => {
      const txt = document.getElementById("sync-import-text").value.trim();
      if (!txt) return toast("请先粘贴同步码或选择 .json 同步文件", "warn");
      const syncMembers = Boolean(document.getElementById("sync-opt-members")?.checked);
      try {
        const res = await api().import_sync_payload(txt, syncMembers);
        const r = res.import_result || {};
        state.settleDraft = null;
        closeModal();
        applyStatePayload(res.state);
        if (r.wallet_created && r.wallet_id) {
          toast(`已创建钱包并同步 ${r.total_checkpoints} 条记录，正在后台拉取最新矿机份额...`);
          api()
            .refresh_wallet(r.wallet_id)
            .then((rf) => {
              if (rf && rf.state) applyStatePayload(rf.state);
            })
            .catch(() => {});
        } else {
          toast(`同步完成：新增 ${r.added_checkpoints} 条 · 更新 ${r.updated_checkpoints} 条，已按最新起点刷新收益`);
        }
      } catch (e) {
        toast(`导入失败: ${e.message || e}`, "error");
      }
    };
  }

  function openCalibratePoolPaidModal(targetCp = null) {
    const wallet = state.data && state.data.active_wallet;
    if (!wallet) return;
    const cps = state.data.checkpoints || [];
    const cp = targetCp || cps[0];
    if (!cp) {
      toast("目前还没有提现/基准记录，已自动按从 0 起算（建好基准或第一次提现后即可校准）", "warn");
      return;
    }
    const ov = wallet.overview || {};
    const totalPaidNow = Number(ov.total_paid_prl || 0);
    const rp = state.data.round_pending_info || {};
    const suggested =
      cp.pool_paid_snapshot != null
        ? Number(cp.pool_paid_snapshot)
        : rp.baseline_pool_paid_prl != null
          ? Number(rp.baseline_pool_paid_prl)
          : "";

    openModal(
      "校准上次提现/基准时的矿池已付基数",
      `
        <div class="alert-box">
          矿池每满 5 PRL 自动打款一次，但如果你多轮才提现一次，首页看板需要用「<strong>当前矿池累计已支付 − 上次提现/基准时的矿池累计已支付</strong>」来计算本轮还没结算分红的 PRL 总量。<br/>
          当前记录：<strong>${escapeHtml(cp.checkpoint_time)}</strong>（${cp.kind === "baseline" ? "基准起点" : "提现结算"}${cp.label ? ` · ${escapeHtml(cp.label)}` : ""}）<br/>
          当前矿池官网累计已支付：<strong>${totalPaidNow.toFixed(4)} PRL</strong>
        </div>
        <div class="form-field">
          <label style="display:flex;justify-content:space-between;align-items:center;">
            <span>该时间点对应的矿池累计已支付总额 (PRL)</span>
            <button type="button" class="link-btn" id="btn-cp-snap-clear" style="padding:0;font-size:12px;">改回自动推算(清空)</button>
          </label>
          <input id="cp-pool-paid-snap" type="number" step="0.000001" min="0" class="input" value="${suggested !== "" ? suggested : ""}" placeholder="例如 60.0000（留空则自动按转账记录推算）" />
        </div>
      `,
      [
        { text: "取消", onClick: closeModal },
        {
          text: "保存并重算待发预估",
          cls: "btn-primary",
          onClick: async () => {
            try {
              const raw = document.getElementById("cp-pool-paid-snap").value.trim();
              let val = null;
              if (raw !== "") {
                val = Number(raw);
                if (!Number.isFinite(val) || val < 0) {
                  return toast("请输入大于等于 0 的有效 PRL 数值", "warn");
                }
              }
              const res = await api().set_checkpoint_pool_snapshot(wallet.id, cp.id, val);
              closeModal();
              applyStatePayload(res.state);
              toast(val !== null ? `已锁定该起点矿池已付快照为 ${val.toFixed(4)} PRL` : "已恢复自动推算");
            } catch (e) {
              toast(`保存失败: ${e.message || e}`, "error");
            }
          },
        },
      ]
    );
    const clrBtn = document.getElementById("btn-cp-snap-clear");
    if (clrBtn) {
      clrBtn.onclick = () => {
        document.getElementById("cp-pool-paid-snap").value = "";
      };
    }
  }

  function openHistoricalBaselineModal(existingCp = null) {
    const wallet = state.data && state.data.active_wallet;
    if (!wallet) return;
    const isEdit = Boolean(existingCp);
    const payments = (wallet.overview && wallet.overview.payments) || [];
    const currentWorkers = state.data.workers || [];
    const liveMap = {};
    currentWorkers.forEach((w) => {
      liveMap[w.worker_name] = w.last_shares;
    });

    const rows = isEdit
      ? (existingCp.workers || []).map((w) => ({
          worker_name: w.worker_name,
          shares: Number(w.curr_shares || 0),
          hint: liveMap[w.worker_name] != null ? `当前在线/缓存: ${fmtInt(liveMap[w.worker_name])}` : "历史矿机",
        }))
      : currentWorkers.map((w) => ({
          worker_name: w.worker_name,
          shares: 0,
          hint: `当前: ${fmtInt(w.last_shares)}`,
        }));

    const defaultTime = isEdit
      ? existingCp.checkpoint_time
      : (payments[0] && payments[0].paid_at) || nowLocalStr();

    const payOpts = payments
      .map(
        (p) =>
          `<option value="${escapeHtml(p.paid_at)}">${escapeHtml(p.paid_at)} (${Number(p.amount_prl).toFixed(4)} PRL)</option>`
      )
      .join("");

    openModal(
      isEdit ? `修改基准快照 (${existingCp.checkpoint_time})` : "手动补录上次提现基准份额",
      `
        <p class="panel-desc" style="margin-bottom:4px;">
          中途启用本软件时，可在这里填写<strong>上次提现时你自己记录的各矿机有效份额</strong>。保存后会作为基准起点，下次结算会自动用「当前份额 - 这里填的份额」计算增量。
        </p>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
          <div class="form-field">
            <label>上次提现时间点 <span class="req">*</span></label>
            <input id="hb-time" class="input" value="${escapeHtml(defaultTime)}" placeholder="YYYY-MM-DD HH:MM:SS" />
          </div>
          <div class="form-field">
            <label>快捷带入矿池打款时间</label>
            <select id="hb-pay-pick" class="input">
              <option value="">-- 可选：从矿池记录选择时间 --</option>
              ${payOpts}
            </select>
          </div>
          <div class="form-field">
            <label>基准标签</label>
            <input id="hb-label" class="input" value="${escapeHtml(isEdit ? existingCp.label : "上次提现手动基准")}" placeholder="例如：11月15日提现前基准" />
          </div>
          <div class="form-field">
            <label>当时矿池累计已付 (PRL · 可选)</label>
            <input id="hb-pool-paid" type="number" step="0.000001" min="0" class="input" value="${isEdit && existingCp.pool_paid_snapshot != null ? existingCp.pool_paid_snapshot : ""}" placeholder="留空自动推算，例如 60.0" />
          </div>
          <div class="form-field" style="grid-column:1 / -1;">
            <label>备注 (可选)</label>
            <input id="hb-notes" class="input" value="${escapeHtml(isEdit ? existingCp.notes : "")}" placeholder="说明或记录来源" />
          </div>
        </div>

        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px;">
          <strong style="font-size:13px;">各矿机在上次提现时的份额</strong>
          <div style="display:flex;gap:8px;">
            <button type="button" class="btn btn-sm btn-secondary" id="hb-toggle-paste">📋 批量粘贴</button>
            <button type="button" class="btn btn-sm btn-secondary" id="hb-add-row">+ 添加一行矿机</button>
          </div>
        </div>

        <div id="hb-paste-box" class="hidden" style="background:var(--bg-elevated);padding:10px;border-radius:7px;border:1px solid var(--border);">
          <div class="setting-desc" style="margin-bottom:6px;">每行输入「矿机名 份额」（空格、制表符或逗号分隔均可），例如：<code>alice_main 35000</code></div>
          <textarea id="hb-paste-text" class="input" rows="4" placeholder="alice_main 35000&#10;alice_rig2 9200&#10;bob_rig1 4100"></textarea>
          <div style="margin-top:6px;text-align:right;">
            <button type="button" class="btn btn-sm btn-primary" id="hb-apply-paste">解析并填入下方列表</button>
          </div>
        </div>

        <div class="table-wrap" style="max-height:260px;overflow-y:auto;border:1px solid var(--border);border-radius:7px;">
          <table class="data-table">
            <thead>
              <tr>
                <th>矿机名称</th>
                <th class="num">上次记录份额</th>
                <th>参考</th>
                <th class="act">删除</th>
              </tr>
            </thead>
            <tbody id="hb-rows-tbody"></tbody>
          </table>
        </div>
      `,
      [
        { text: "取消", onClick: closeModal },
        {
          text: isEdit ? "保存基准修改" : "保存并设为当前结算基准",
          cls: "btn-primary",
          onClick: async () => {
            try {
              const entries = [];
              document.querySelectorAll("#hb-rows-tbody tr").forEach((tr) => {
                const name = tr.querySelector(".hb-name").value.trim();
                const shares = Math.max(0, parseInt(tr.querySelector(".hb-shares").value || "0", 10));
                if (name) {
                  entries.push({
                    worker_name: name,
                    curr_shares: shares,
                    share_source: "manual",
                  });
                }
              });
              if (!entries.length) {
                toast("请至少填写一台矿机及其份额", "error");
                return;
              }
              const timeVal = document.getElementById("hb-time").value.trim() || nowLocalStr();
              const labelVal = document.getElementById("hb-label").value.trim();
              const notesVal = document.getElementById("hb-notes").value.trim();
              const rawSnap = document.getElementById("hb-pool-paid")?.value?.trim() || "";
              let snapVal = null;
              if (rawSnap !== "") {
                snapVal = Number(rawSnap);
                if (!Number.isFinite(snapVal) || snapVal < 0) {
                  toast("当时矿池累计已付金额无效", "warn");
                  return;
                }
              }

              if (isEdit) {
                const res = await api().update_baseline_checkpoint({
                  checkpoint_id: existingCp.id,
                  checkpoint_time: timeVal,
                  label: labelVal,
                  notes: notesVal,
                  pool_paid_snapshot: snapVal,
                  worker_entries: entries,
                });
                state.settleDraft = null;
                closeModal();
                applyStatePayload(res.state);
                toast("已更新历史基准快照");
              } else {
                const res = await api().save_checkpoint_and_settle({
                  wallet_id: wallet.id,
                  kind: "baseline",
                  checkpoint_time: timeVal,
                  label: labelVal || "上次提现手动基准",
                  prl_amount: 0,
                  cny_amount: 0,
                  tx_hash: "",
                  notes: notesVal,
                  prev_checkpoint_id: null,
                  pool_paid_snapshot: snapVal,
                  worker_entries: entries,
                });
                state.settleDraft = null;
                state.settlePrevCheckpointId = res.saved.id;
                closeModal();
                applyStatePayload(res.state);
                if (state.currentView === "settle") await initSettleDraft();
                toast("已保存历史基准，并自动设为结算起始点！");
              }
            } catch (e) {
              toast(String(e), "error");
            }
          },
        },
      ],
      true
    );

    const renderHbRows = () => {
      const tbody = document.getElementById("hb-rows-tbody");
      if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="4" class="empty-state">暂无矿机行，点击右上角「+ 添加一行矿机」</td></tr>`;
        return;
      }
      tbody.innerHTML = rows
        .map(
          (r, idx) => `
          <tr>
            <td><input type="text" class="input-sm hb-name" data-idx="${idx}" value="${escapeHtml(r.worker_name)}" placeholder="矿机名如 bob_01" style="width:165px;" /></td>
            <td class="num"><input type="number" min="0" class="input inline-share-input hb-shares" data-idx="${idx}" value="${r.shares}" /></td>
            <td style="font-size:11.5px;color:var(--text-dim);">${escapeHtml(r.hint || "")}</td>
            <td class="act"><button type="button" class="link-btn danger hb-del" data-idx="${idx}">移除</button></td>
          </tr>
        `
        )
        .join("");

      tbody.querySelectorAll(".hb-name").forEach((inp) => {
        inp.oninput = () => {
          rows[Number(inp.dataset.idx)].worker_name = inp.value;
        };
      });
      tbody.querySelectorAll(".hb-shares").forEach((inp) => {
        inp.oninput = () => {
          rows[Number(inp.dataset.idx)].shares = Math.max(0, parseInt(inp.value || "0", 10));
        };
      });
      tbody.querySelectorAll(".hb-del").forEach((btn) => {
        btn.onclick = () => {
          rows.splice(Number(btn.dataset.idx), 1);
          renderHbRows();
        };
      });
    };

    renderHbRows();

    document.getElementById("hb-pay-pick").onchange = (e) => {
      if (e.target.value) document.getElementById("hb-time").value = e.target.value;
    };
    document.getElementById("hb-add-row").onclick = () => {
      rows.push({ worker_name: "", shares: 0, hint: "新增历史矿机" });
      renderHbRows();
    };
    document.getElementById("hb-toggle-paste").onclick = () => {
      document.getElementById("hb-paste-box").classList.toggle("hidden");
    };
    document.getElementById("hb-apply-paste").onclick = () => {
      const raw = document.getElementById("hb-paste-text").value || "";
      const lines = raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      let applied = 0;
      lines.forEach((line) => {
        const parts = line.split(/[\s,，\t:：]+/).filter(Boolean);
        if (parts.length >= 2) {
          const wName = parts[0];
          const val = parseInt(parts[1].replace(/,/g, ""), 10);
          if (!Number.isNaN(val)) {
            const existing = rows.find((r) => r.worker_name.toLowerCase() === wName.toLowerCase());
            if (existing) {
              existing.shares = Math.max(0, val);
            } else {
              rows.push({
                worker_name: wName,
                shares: Math.max(0, val),
                hint: liveMap[wName] != null ? `当前: ${fmtInt(liveMap[wName])}` : "粘贴导入",
              });
            }
            applied += 1;
          }
        }
      });
      renderHbRows();
      toast(`已解析并填入 ${applied} 条矿机份额`);
    };
  }

  function openMergePaymentsModal() {
    if (!state.data?.active_wallet) return;
    const ov = state.data.active_wallet.overview || {};
    const payments = ov.payments || [];
    const priceCny = Number(ov.prl_price_cny || 0);
    if (!payments.length) {
      toast("当前暂无矿池打款记录，请先点击「立即拉取」同步矿池数据", "warn");
      return;
    }

    const checkpoints = state.data.checkpoints || [];
    const prevSelId = Number(document.getElementById("settle-prev-checkpoint")?.value || 0);
    const chosenCp =
      checkpoints.find((c) => Number(c.id) === prevSelId) ||
      (checkpoints.length > 0 ? checkpoints[0] : null);
    const baselineTime = chosenCp ? chosenCp.checkpoint_time : "";

    const selectedSet = new Set();
    payments.forEach((p, idx) => {
      if (!baselineTime || String(p.paid_at || "") > String(baselineTime)) {
        selectedSet.add(idx);
      }
    });
    if (selectedSet.size === 0 && payments.length > 0) {
      selectedSet.add(0);
    }

    const renderPaymentRows = () =>
      payments
        .map((p, idx) => {
          const isNew = !baselineTime || String(p.paid_at || "") > String(baselineTime);
          const checked = selectedSet.has(idx) ? "checked" : "";
          const badge = isNew
            ? `<span class="badge-pill badge-online">本轮新打款</span>`
            : `<span class="badge-pill" style="background:rgba(148,163,184,0.12); color:var(--text-muted)">早于基准</span>`;
          return `
            <tr class="mp-row" data-idx="${idx}" style="cursor:pointer">
              <td><input type="checkbox" class="mp-cb" data-idx="${idx}" ${checked} /></td>
              <td class="mono">${esc(p.paid_at)}</td>
              <td>${badge}</td>
              <td class="num mono c-gold" style="font-weight:700">${Number(p.amount_prl).toFixed(6)} PRL</td>
              <td class="mono c-muted" style="font-size:11.5px">${esc((p.tx_hash || "").slice(0, 18))}...</td>
            </tr>
          `;
        })
        .join("");

    openModal(
      "⚡ 选择/合并多笔矿池打款记录",
      `
      <div class="alert-box" style="margin-bottom:10px">
        💡 如果矿池已经给您的钱包打了多次币，您现在才一起卖币提现分红，请勾选这期间包含的<strong>所有矿池打款记录</strong>。系统会自动把所选打款的 PRL 数量累加合并！
        ${
          chosenCp
            ? `<br/><span class="c-muted">当前对比基准时间：<strong class="mono">${esc(chosenCp.checkpoint_time)}</strong>（已自动为您预勾选晚于该基准的打款）</span>`
            : ""
        }
      </div>
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; gap:8px; flex-wrap:wrap">
        <div id="mp-summary-text" class="pay-merge-summary"></div>
        <div class="btn-group">
          <button type="button" class="btn btn-xs btn-ghost" id="mp-btn-select-new">仅选本轮新打款</button>
          <button type="button" class="btn btn-xs btn-ghost" id="mp-btn-select-all">全选</button>
          <button type="button" class="btn btn-xs btn-ghost" id="mp-btn-clear">清空</button>
        </div>
      </div>
      <div class="table-wrap" style="max-height:300px; overflow-y:auto; border:1px solid var(--border); border-radius:8px">
        <table class="data-table compact">
          <thead>
            <tr>
              <th style="width:36px">选</th>
              <th>打款时间</th>
              <th>状态</th>
              <th class="num">打款数量 (PRL)</th>
              <th>交易哈希</th>
            </tr>
          </thead>
          <tbody id="mp-tbody">${renderPaymentRows()}</tbody>
        </table>
      </div>
      <div style="margin-top:10px; display:flex; align-items:center; gap:8px; font-size:12.5px; color:var(--text-muted)">
        <input type="checkbox" id="mp-fill-est-cny" />
        <label for="mp-fill-est-cny" style="cursor:pointer">同时按当前参考币价 (${priceCny > 0 ? `￥${priceCny.toFixed(2)}/PRL` : "暂无"}) 自动估算并填入人民币金额（仅在人民币栏为空时填入）</label>
      </div>
      <div class="modal-footer">
        <button class="btn btn-ghost" id="mp-cancel">取消</button>
        <button class="btn btn-primary" id="mp-confirm">⚡ 带入合并结果</button>
      </div>
      `
    );

    const updateMpSummary = () => {
      const chosen = Array.from(selectedSet)
        .sort((a, b) => a - b)
        .map((i) => payments[i])
        .filter(Boolean);
      const totalPrl = chosen.reduce((acc, p) => acc + Number(p.amount_prl || 0), 0);
      const estCny = priceCny > 0 ? ` (≈￥${(totalPrl * priceCny).toFixed(2)})` : "";
      const sumEl = document.getElementById("mp-summary-text");
      if (sumEl) {
        sumEl.textContent = `已选 ${chosen.length} 笔打款 · 合计 ${totalPrl.toFixed(6)} PRL${estCny}`;
      }
      const confirmBtn = document.getElementById("mp-confirm");
      if (confirmBtn) confirmBtn.disabled = chosen.length === 0;
    };

    const rebindRows = () => {
      const tbody = document.getElementById("mp-tbody");
      if (!tbody) return;
      tbody.innerHTML = renderPaymentRows();
      tbody.querySelectorAll(".mp-cb").forEach((cb) => {
        cb.onclick = (e) => e.stopPropagation();
        cb.onchange = (e) => {
          const idx = Number(cb.dataset.idx);
          if (e.target.checked) selectedSet.add(idx);
          else selectedSet.delete(idx);
          updateMpSummary();
        };
      });
      tbody.querySelectorAll(".mp-row").forEach((row) => {
        row.onclick = () => {
          const idx = Number(row.dataset.idx);
          if (selectedSet.has(idx)) selectedSet.delete(idx);
          else selectedSet.add(idx);
          rebindRows();
          updateMpSummary();
        };
      });
    };

    rebindRows();
    updateMpSummary();

    document.getElementById("mp-btn-select-new").onclick = () => {
      selectedSet.clear();
      payments.forEach((p, idx) => {
        if (!baselineTime || String(p.paid_at || "") > String(baselineTime)) {
          selectedSet.add(idx);
        }
      });
      rebindRows();
      updateMpSummary();
    };
    document.getElementById("mp-btn-select-all").onclick = () => {
      payments.forEach((_, idx) => selectedSet.add(idx));
      rebindRows();
      updateMpSummary();
    };
    document.getElementById("mp-btn-clear").onclick = () => {
      selectedSet.clear();
      rebindRows();
      updateMpSummary();
    };
    document.getElementById("mp-cancel").onclick = closeModal;
    document.getElementById("mp-confirm").onclick = async () => {
      const chosen = Array.from(selectedSet)
        .sort((a, b) => a - b)
        .map((i) => payments[i])
        .filter(Boolean);
      const merged = buildMergedPaymentsPayload(chosen);
      if (!merged) return;

      document.getElementById("settle-time").value = merged.paid_at;
      document.getElementById("settle-prl").value = merged.amount_prl;
      document.getElementById("settle-label").value = merged.label;
      document.getElementById("settle-notes").value = merged.notes;

      const fillEst = document.getElementById("mp-fill-est-cny")?.checked;
      const cnyInput = document.getElementById("settle-cny");
      if (fillEst && priceCny > 0 && cnyInput && !Number(cnyInput.value || 0)) {
        cnyInput.value = (merged.amount_prl * priceCny).toFixed(2);
      }

      closeModal();
      await recomputeSettlePreview();
      toast(
        merged.count > 1
          ? `已合并 ${merged.count} 笔矿池打款，共计 ${merged.amount_prl} PRL`
          : `已带入矿池打款 ${merged.amount_prl} PRL`
      );
    };
  }

  // -------------------------------------------------------------------
  // Local SRBMiner Binder & Collector Cluster View
  // -------------------------------------------------------------------
  let lmInspectedBatPath = "";
  let lmScannedMap = new Map();
  let colDetailLogs = [];

  function parseHashrateThsText(text) {
    const raw = String(text || "").trim();
    if (!raw) return 0;
    const m = raw.match(
      /([0-9]+(?:\.[0-9]+)?)\s*(ph\/s|p|th\/s|t|gh\/s|g|mh\/s|m|kh\/s|k|h\/s|h)?/i
    );
    if (!m) return 0;
    const val = parseFloat(m[1]);
    if (!Number.isFinite(val) || val <= 0) return 0;
    const unit = (m[2] || "t").toLowerCase();
    if (unit.startsWith("p")) return val * 1000;
    if (unit.startsWith("t")) return val;
    if (unit.startsWith("g")) return val / 1000;
    if (unit.startsWith("m")) return val / 1e6;
    if (unit.startsWith("k")) return val / 1e9;
    return val / 1e12;
  }

  function formatUptime(sec) {
    const s = Math.max(0, Math.floor(Number(sec || 0)));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m ${String(r).padStart(2, "0")}s`;
    if (m > 0) return `${m}m ${String(r).padStart(2, "0")}s`;
    return `${r}s`;
  }

  function buildHashrateSparklineSvg(samples, compareThs = 0) {
    const pts = (Array.isArray(samples) ? samples : []).filter((s) => Number(s && s.ths) > 0);
    if (!pts.length) {
      return `<span class="c-muted">等待内核输出算力采样点...</span>`;
    }
    if (pts.length === 1) {
      return `<span class="c-pri mono">已捕获 1 个采样点: ${Number(pts[0].ths).toFixed(2)} TH/s (${escapeHtml(pts[0].time || "")})</span>`;
    }
    const w = 480;
    const h = 82;
    const padX = 8;
    const padY = 14;
    const allVals = pts.map((p) => Number(p.ths));
    if (compareThs > 0) allVals.push(compareThs);
    const minV = Math.min(...allVals);
    const maxV = Math.max(...allVals);
    const span = Math.max(maxV - minV, Math.max(maxV * 0.05, 0.1));
    const lo = Math.max(0, minV - span * 0.15);
    const hi = maxV + span * 0.18;

    const toY = (v) => {
      const ratio = (v - lo) / Math.max(hi - lo, 1e-6);
      return h - 8 - ratio * (h - padY - 10);
    };
    const toX = (idx) => padX + (idx / (pts.length - 1)) * (w - padX * 2);

    const coords = pts.map((p, i) => [toX(i), toY(Number(p.ths))]);
    const linePoints = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
    const areaPoints = `${coords[0][0].toFixed(1)},${h} ${linePoints} ${coords[coords.length - 1][0].toFixed(1)},${h}`;
    const last = coords[coords.length - 1];
    const rawMin = Math.min(...pts.map((p) => Number(p.ths)));
    const rawMax = Math.max(...pts.map((p) => Number(p.ths)));
    const firstTime = escapeHtml(pts[0].time || "");
    const lastTime = escapeHtml(pts[pts.length - 1].time || "");
    const gradId = `spg-${Math.random().toString(36).slice(2, 8)}`;

    let refLine = "";
    if (compareThs > 0) {
      const cy = toY(compareThs);
      if (cy >= 6 && cy <= h - 4) {
        refLine = `<line x1="${padX}" y1="${cy.toFixed(1)}" x2="${w - padX}" y2="${cy.toFixed(1)}" stroke="#f59e0b" stroke-width="1" stroke-dasharray="4 3" opacity="0.75"/>`;
      }
    }

    return `
      <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:100%;display:block;">
        <defs>
          <linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.35"/>
            <stop offset="100%" stop-color="#38bdf8" stop-opacity="0.02"/>
          </linearGradient>
        </defs>
        ${refLine}
        <polygon points="${areaPoints}" fill="url(#${gradId})"/>
        <polyline points="${linePoints}" fill="none" stroke="#38bdf8" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="3.5" fill="#34d399" stroke="#060a0e" stroke-width="1.5"/>
        <text x="6" y="12" fill="#94a3b8" font-size="10" font-family="Consolas, monospace">Peak ${rawMax.toFixed(2)} / Min ${rawMin.toFixed(2)} TH/s</text>
        <text x="${w - 6}" y="12" text-anchor="end" fill="#64748b" font-size="10" font-family="Consolas, monospace">${firstTime} - ${lastTime} (${pts.length}点)</text>
      </svg>
    `;
  }

  function renderTerminalBox(containerEl, lines, filterText, emptyMsg, autoscroll = true) {
    if (!containerEl) return;
    const needle = String(filterText || "").trim().toLowerCase();
    const filtered = needle
      ? (lines || []).filter((it) => String(it.line || "").toLowerCase().includes(needle))
      : lines || [];
    if (!filtered.length) {
      containerEl.innerHTML = `<div class="c-muted">${escapeHtml(
        needle ? "没有匹配关键词的日志行" : emptyMsg
      )}</div>`;
      return;
    }
    const validLevels = new Set(["share", "hashrate", "warn", "error", "pool", "info"]);
    const nearBottom =
      containerEl.scrollHeight - containerEl.scrollTop - containerEl.clientHeight < 48;
    containerEl.innerHTML = filtered
      .map((it) => {
        const lvl = validLevels.has(it.level) ? it.level : "info";
        const rawLine = String(it.line || "");
        const hasOwnTs = /^\[?\d{2,4}[-/:]/.test(rawLine);
        const rawTs = it.ts ? String(it.ts) : "";
        const shortTs = rawTs.length > 12 ? rawTs.slice(11, 19) : rawTs;
        const tsPrefix =
          !hasOwnTs && shortTs
            ? `<span class="c-muted">[${escapeHtml(shortTs)}]</span> `
            : "";
        return `<div class="term-line term-${lvl}">${tsPrefix}${escapeHtml(rawLine)}</div>`;
      })
      .join("");
    if (autoscroll && (nearBottom || filtered.length <= 40)) {
      containerEl.scrollTop = containerEl.scrollHeight;
    }
  }

  function renderDevicesTable(tbodyEl, devices, includeShares = true) {
    if (!tbodyEl) return;
    const list = Array.isArray(devices) ? devices : [];
    const colspan = includeShares ? 8 : 7;
    if (!list.length) {
      tbodyEl.innerHTML = `<tr><td colspan="${colspan}" class="empty-state">等待分卡/设备算力输出...</td></tr>`;
      return;
    }
    tbodyEl.innerHTML = list
      .map((d) => {
        const avgTxt =
          Number(d.avg_hashrate_ths || 0) > 0
            ? `${Number(d.avg_hashrate_ths).toFixed(2)} TH/s`
            : "--";
        const tempTxt = d.temp_c != null ? `${d.temp_c}°C` : "--";
        const fanTxt = d.fan_pct != null ? `${d.fan_pct}%` : "--";
        const powTxt = d.power_w != null ? `${Number(d.power_w).toFixed(1)}W` : "--";
        const tipParts = [d.name || d.id || ""];
        if (d.eff_value != null) tipParts.push(`能效: ${Number(d.eff_value).toFixed(3)}`);
        if (d.cclk_mhz != null) tipParts.push(`核心: ${d.cclk_mhz}MHz`);
        if (d.mclk_mhz != null) tipParts.push(`显存: ${d.mclk_mhz}MHz`);
        const shareCol = includeShares
          ? `<td class="num mono">${fmtInt(d.accepted_shares || 0)}</td>`
          : "";
        return `
          <tr title="${escapeHtml(tipParts.join(" · "))}">
            <td class="mono">${escapeHtml(d.id || "GPU")}</td>
            <td style="max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(d.name || "--")}</td>
            <td class="num mono c-pri" style="font-weight:700">${escapeHtml(d.hashrate_text || "--")}</td>
            <td class="num mono">${avgTxt}</td>
            <td class="num mono">${tempTxt}</td>
            <td class="num mono">${fanTxt}</td>
            <td class="num mono">${powTxt}</td>
            ${shareCol}
          </tr>
        `;
      })
      .join("");
  }

  function setLocalMinerMode(mode) {
    state.lmMode = mode;
    document.querySelectorAll("#lm-mode-pills .pill").forEach((b) => {
      b.classList.toggle("active", b.dataset.lmode === mode);
    });
    document.getElementById("lm-ctrl-bat")?.classList.toggle("hidden", mode !== "bat");
    document.getElementById("lm-ctrl-pid")?.classList.toggle("hidden", mode !== "pid");
    document.getElementById("lm-ctrl-log")?.classList.toggle("hidden", mode !== "log");
    if (mode === "pid") {
      const sel = document.getElementById("lm-pid-select");
      if (sel && sel.options.length <= 1) {
        scanLocalMinersList();
      }
    }
  }

  async function inspectBatPreview(batPath) {
    const bar = document.getElementById("lm-bat-parsed-bar");
    if (!bar) return;
    const cleaned = String(batPath || "").trim();
    if (!cleaned) {
      bar.classList.add("hidden");
      bar.innerHTML = "";
      return;
    }
    try {
      lmInspectedBatPath = cleaned;
      const info = await api().inspect_bat_file(cleaned);
      if (info.error) {
        bar.innerHTML = `<span style="color:#f87171;">⚠️ ${escapeHtml(info.error)}</span>`;
        bar.classList.remove("hidden");
        return;
      }
      const chips = [];
      if (info.algorithm) chips.push(`<span>算法: <strong>${escapeHtml(info.algorithm)}</strong></span>`);
      if (info.worker) chips.push(`<span>矿工名: <strong>${escapeHtml(info.worker)}</strong></span>`);
      if (info.pool) chips.push(`<span>矿池: <span class="mono">${escapeHtml(info.pool)}</span></span>`);
      if (info.log_file) {
        chips.push(`<span>原日志: <span class="mono">${escapeHtml(info.log_file)}</span></span>`);
      } else {
        chips.push(`<span class="c-gold">原脚本未设 --log-file，启动时将自动临时注入</span>`);
      }
      if (info.uses_start) {
        chips.push(`<span class="c-gold">检测到 start 命令，监听时将自动前景直启以抓取实时输出</span>`);
      }
      if (!chips.length) {
        chips.push(`<span>已加载脚本 ${escapeHtml(info.bat_name || cleaned)}</span>`);
      }
      bar.innerHTML = chips.join('<span class="c-muted">|</span>');
      bar.classList.remove("hidden");
    } catch (e) {
      bar.classList.add("hidden");
    }
  }

  async function scanLocalMinersList() {
    const sel = document.getElementById("lm-pid-select");
    if (!sel) return;
    sel.innerHTML = `<option value="">正在扫描本机 SRBMiner 进程...</option>`;
    try {
      const list = await api().scan_local_miners();
      lmScannedMap.clear();
      if (!list || !list.length) {
        sel.innerHTML = `<option value="">未发现正在运行的 SRBMiner 进程（请确认矿机已启动，或改用「绑定 .bat 启动」）</option>`;
        toast("未发现运行中的 SRBMiner 进程", "warn");
        return;
      }
      sel.innerHTML = list
        .map((p) => {
          lmScannedMap.set(String(p.pid), p);
          const tag = p.is_primary_miner ? "主挖矿进程" : "父/监控进程";
          const logTag = p.detected_log_file || p.log_file ? "含日志文件" : "控制台直读";
          const batPart = p.parent_bat ? ` · ${p.parent_bat}` : "";
          const label = `[PID ${p.pid}] ${p.worker || p.name} (${p.algorithm || "unknown"}) [${tag} / ${logTag}]${batPart}`;
          return `<option value="${p.pid}">${escapeHtml(label)}</option>`;
        })
        .join("");
      const primary = list.find((p) => p.is_primary_miner) || list[0];
      if (primary) sel.value = String(primary.pid);
      toast(`已扫描到 ${list.length} 个相关 SRBMiner 进程`);
    } catch (e) {
      sel.innerHTML = `<option value="">扫描失败: ${escapeHtml(e.message || e)}</option>`;
      toast(`扫描进程失败: ${e.message || e}`, "error");
    }
  }

  function syncLocalMinerPrefs() {
    const settings = (state.data && state.data.settings) || {};
    const show = settings.show_local_miner_panel === "1";
    const toggleEl = document.getElementById("toggle-local-miner-panel");
    const bodyEl = document.getElementById("local-miner-body");
    if (toggleEl) toggleEl.checked = show;
    if (bodyEl) bodyEl.classList.toggle("hidden", !show);

    const batEl = document.getElementById("lm-bat-path");
    if (batEl && document.activeElement !== batEl && settings.local_bat_path !== undefined) {
      batEl.value = settings.local_bat_path || "";
    }
    const logEl = document.getElementById("lm-log-path");
    if (logEl && document.activeElement !== logEl && settings.local_log_path !== undefined) {
      logEl.value = settings.local_log_path || "";
    }
    const pushEl = document.getElementById("lm-push-enabled");
    if (pushEl) pushEl.checked = settings.telemetry_push_enabled === "1";
    const cnameEl = document.getElementById("lm-client-name");
    if (cnameEl && document.activeElement !== cnameEl && settings.telemetry_client_name !== undefined) {
      cnameEl.value = settings.telemetry_client_name || "";
    }

    if (show && settings.local_bat_path && lmInspectedBatPath !== settings.local_bat_path) {
      inspectBatPreview(settings.local_bat_path);
    }
    if (show) {
      pollLocalMinerStatus(true);
    }
  }

  function renderLocalMinerStatus() {
    const st = state.lmLastStatus;
    if (!st) return;
    const rep = st.report || {};
    const badge = document.getElementById("lm-status-badge");
    const modeInfo = document.getElementById("lm-mode-info");
    const statusMap = {
      idle: ["未绑定", "status-missing", "background:rgba(255,255,255,0.07);color:var(--text-muted)"],
      running: ["实时监听中", "status-online", ""],
      stopped: ["已停止", "status-missing", "background:rgba(148,163,184,0.12);color:#94a3b8"],
      exited: ["进程已退出", "status-offline", ""],
      error: ["绑定异常", "status-missing", ""],
    };
    const [bText, bCls, bStyle] = statusMap[st.status] || statusMap.idle;
    if (badge) {
      badge.textContent = bText;
      badge.className = `status-badge ${bCls}`;
      badge.setAttribute("style", bStyle);
    }

    const modeLabels = {
      none: "",
      bat_managed: "托管 .bat 实时输出",
      pid_attach: "挂载运行中 PID",
      log_tail: "实时追踪日志文件",
    };
    if (modeInfo) {
      const pidTxt = st.attached_pid ? `PID ${st.attached_pid}` : "";
      const errTxt = st.last_error ? ` · ⚠️ ${st.last_error}` : "";
      modeInfo.textContent = [modeLabels[st.mode], st.source_label, pidTxt].filter(Boolean).join(" · ") + errTxt;
      modeInfo.style.color = st.last_error ? "#f87171" : "";
    }

    const isRun = st.status === "running";
    document.getElementById("btn-lm-stop")?.classList.toggle("hidden", !(st.mode === "bat_managed" && isRun));
    document.getElementById("btn-lm-detach")?.classList.toggle("hidden", !(st.mode === "pid_attach" && isRun));
    document.getElementById("btn-lm-stop-log")?.classList.toggle("hidden", !(st.mode === "log_tail" && isRun));

    // Push status
    const pushStEl = document.getElementById("lm-push-status");
    const pushInfo = st.push || {};
    if (pushStEl) {
      if (pushInfo.last_push_error) {
        pushStEl.textContent = `上报失败: ${pushInfo.last_push_error}`;
        pushStEl.style.color = "#f87171";
      } else if (pushInfo.last_push_at) {
        pushStEl.textContent = `已上报 ${pushInfo.push_count || 0} 次 · 最近 ${pushInfo.last_push_at}`;
        pushStEl.style.color = "#34d399";
      } else {
        const pushOn = document.getElementById("lm-push-enabled")?.checked;
        pushStEl.textContent = pushOn ? "已开启自动上报 (每 3s)" : "未开启上报";
        pushStEl.style.color = "";
      }
    }

    // Real vs Pool comparison
    const ov = (state.data && state.data.active_wallet && state.data.active_wallet.overview) || {};
    const poolLiveThs = parseHashrateThsText(ov.live_hashrate);
    const currThs = Number(rep.current_hashrate_ths || 0);

    document.getElementById("lm-stat-curr").textContent = rep.current_hashrate_text || "0.00 TH/s";
    const diffEl = document.getElementById("lm-stat-pool-diff");
    if (diffEl) {
      if (currThs > 0 && poolLiveThs > 0) {
        const diff = currThs - poolLiveThs;
        const pct = (currThs / poolLiveThs) * 100;
        diffEl.textContent = `矿池实时 ${ov.live_hashrate} · 本机占比 ${pct.toFixed(1)}% (${diff >= 0 ? "+" : ""}${diff.toFixed(2)} TH/s)`;
      } else if (rep.last_hashrate_at) {
        diffEl.textContent = `最近算力更新: ${rep.last_hashrate_at}`;
      } else {
        diffEl.textContent = "等待矿机输出算力行...";
      }
    }

    document.getElementById("lm-stat-avg").textContent = rep.session_avg_text || "0.00 TH/s";
    const fThs = (v) => (Number(v) > 0 ? `${Number(v).toFixed(2)}` : "--");
    const hasKernelAvgs =
      Number(rep.miner_avg_1m_ths || 0) > 0 ||
      Number(rep.miner_avg_15m_ths || 0) > 0 ||
      Number(rep.miner_avg_1h_ths || 0) > 0 ||
      Number(rep.miner_avg_6h_ths || 0) > 0;
    document.getElementById("lm-stat-avg-windows").textContent = hasKernelAvgs
      ? `内核 1m:${fThs(rep.miner_avg_1m_ths)} · 15m:${fThs(rep.miner_avg_15m_ths)} · 1h:${fThs(rep.miner_avg_1h_ths)} · 6h:${fThs(rep.miner_avg_6h_ths)} TH/s`
      : `5m: ${fThs(rep.avg_5m_ths)} · 15m: ${fThs(rep.avg_15m_ths)} · 1h: ${fThs(rep.avg_1h_ths)} TH/s`;

    document.getElementById("lm-stat-stability").textContent =
      Number(rep.stability_pct) > 0 ? `${Number(rep.stability_pct).toFixed(1)}%` : "--%";
    document.getElementById("lm-stat-minmax").textContent =
      `最低 ${Number(rep.min_hashrate_ths) > 0 ? Number(rep.min_hashrate_ths).toFixed(2) : "--"} / 峰值 ${Number(rep.max_hashrate_ths) > 0 ? Number(rep.max_hashrate_ths).toFixed(2) : "--"} TH/s (${rep.sample_count || 0}点)`;

    document.getElementById("lm-stat-shares").textContent =
      `${fmtInt(rep.accepted_shares || 0)} / ${fmtInt(rep.rejected_shares || 0)}`;
    const latStr = Number(rep.avg_latency_ms) > 0 ? `${Math.round(rep.avg_latency_ms)}ms` : "--";
    document.getElementById("lm-stat-share-meta").textContent =
      `验收率 ${Number(rep.accept_rate_pct ?? 100).toFixed(2)}% · 延迟 ${latStr} · ${Number(rep.shares_per_hour || 0).toFixed(1)}/h`;

    const powW = Number(rep.total_power_w || 0);
    const effThw = Number(rep.efficiency_th_per_w || 0);
    const effKw = Number(rep.efficiency_ths_per_kw || 0);
    document.getElementById("lm-stat-power").textContent =
      powW > 0
        ? `${powW.toFixed(1)} W${effThw > 0 ? ` (${effThw.toFixed(3)} Eff)` : effKw > 0 ? ` (${effKw.toFixed(1)} TH/kW)` : ""}`
        : "-- W";
    const runDur = rep.miner_uptime_text || formatUptime(rep.uptime_sec);
    document.getElementById("lm-stat-uptime").textContent =
      `运行 ${runDur} · 断线 ${rep.pool_disconnects || 0} · HW:${rep.hw_errors || 0}`;

    document.getElementById("lm-meta-algo").textContent =
      `算法: ${rep.algorithm || "--"} · 矿池: ${rep.pool_url || "--"}${rep.worker_name ? ` · 矿工: ${rep.worker_name}` : ""}`;

    const sparkWrap = document.getElementById("lm-sparkline-wrap");
    if (sparkWrap) {
      sparkWrap.innerHTML = buildHashrateSparklineSvg(rep.samples || [], poolLiveThs);
    }

    renderDevicesTable(document.getElementById("lm-devices-tbody"), rep.devices || [], true);

    const filterTxt = document.getElementById("lm-log-filter")?.value || "";
    const autoScroll = document.getElementById("lm-autoscroll")?.checked !== false;
    renderTerminalBox(
      document.getElementById("lm-terminal"),
      state.lmLines,
      filterTxt,
      "暂无输出，请绑定并启动 .bat 或挂载运行中的 SRBMiner...",
      autoScroll
    );
  }

  async function pollLocalMinerStatus(force = false) {
    if (state.lmPolling) return;
    const panelOpen = document.getElementById("toggle-local-miner-panel")?.checked;
    const wasRunning = state.lmLastStatus && state.lmLastStatus.status === "running";
    if (!force && !panelOpen && !wasRunning) return;

    state.lmPolling = true;
    try {
      const st = await api().get_local_miner_status(state.lmLatestSeq);
      const rep = st.report || {};
      const newSeq = Number(rep.latest_seq || 0);
      if (newSeq < state.lmLatestSeq) {
        state.lmLines = Array.isArray(rep.lines) ? rep.lines : [];
      } else if (Array.isArray(rep.lines) && rep.lines.length) {
        for (const item of rep.lines) {
          if (Number(item.seq || 0) > state.lmLatestSeq) {
            state.lmLines.push(item);
          }
        }
        if (state.lmLines.length > 1200) {
          state.lmLines = state.lmLines.slice(-1200);
        }
      }
      state.lmLatestSeq = newSeq;
      state.lmLastStatus = st;
      renderLocalMinerStatus();
    } catch (_) {
      // Quietly ignore transient poll errors
    } finally {
      state.lmPolling = false;
    }
  }

  function applyLocalBinderActionResult(st) {
    const rep = (st && st.report) || {};
    state.lmLines = Array.isArray(rep.lines) ? rep.lines : [];
    state.lmLatestSeq = Number(rep.latest_seq || 0);
    state.lmLastStatus = st;
    renderLocalMinerStatus();
  }

  // -------------------------------------------------------------------
  // Collector Cluster Server & Distributed Clients
  // -------------------------------------------------------------------
  function renderCollectorPrefs() {
    if (!state.data) return;
    const srv = state.data.collector_server || {};
    const settings = state.data.settings || {};

    const badge = document.getElementById("col-local-badge");
    if (badge) {
      if (srv.running) {
        badge.textContent = `运行中 :${srv.port}`;
        badge.className = "status-badge status-online";
        badge.removeAttribute("style");
      } else {
        badge.textContent = srv.last_error ? "启动失败" : "未启动";
        badge.className = "status-badge status-missing";
        badge.setAttribute("style", "background:rgba(255,255,255,0.07);color:var(--text-muted)");
      }
    }

    document.getElementById("btn-col-start-local")?.classList.toggle("hidden", Boolean(srv.running));
    document.getElementById("btn-col-stop-local")?.classList.toggle("hidden", !srv.running);
    document.getElementById("btn-col-open-web")?.classList.toggle("hidden", !srv.running);

    const portEl = document.getElementById("col-local-port");
    if (portEl && document.activeElement !== portEl) {
      portEl.value = settings.local_collector_port || srv.port || "8765";
    }
    const tokEl = document.getElementById("col-local-token");
    if (tokEl && document.activeElement !== tokEl) {
      tokEl.value = settings.collector_token || "";
    }
    const modeEl = document.getElementById("col-source-mode");
    if (modeEl && document.activeElement !== modeEl) {
      modeEl.value = settings.collector_mode || "local";
    }
    const remUrlEl = document.getElementById("col-remote-url");
    if (remUrlEl && document.activeElement !== remUrlEl) {
      remUrlEl.value = settings.collector_server_url || `http://127.0.0.1:${settings.local_collector_port || 8765}`;
    }
    const remTokEl = document.getElementById("col-remote-token");
    if (remTokEl && document.activeElement !== remTokEl) {
      remTokEl.value = settings.collector_token || "";
    }

    const lanBox = document.getElementById("col-lan-urls-box");
    if (lanBox) {
      if (srv.running) {
        const urls = (srv.lan_urls && srv.lan_urls.length ? srv.lan_urls : [srv.local_url]).filter(Boolean);
        lanBox.innerHTML =
          `<span>✅ 局域网/公网客户端上报地址:</span>` +
          urls
            .map(
              (u) =>
                `<button type="button" class="prefix-chip mono" data-copy-url="${escapeHtml(u)}" title="点击复制此 URL">${escapeHtml(u)} 📋</button>`
            )
            .join("") +
          (srv.token_enabled ? `<span class="status-badge status-manual">已开启 Token 鉴权</span>` : `<span class="c-muted">未设 Token</span>`);
        lanBox.querySelectorAll("[data-copy-url]").forEach((btn) => {
          btn.onclick = () => copyText(btn.dataset.copyUrl, `已复制地址: ${btn.dataset.copyUrl}`);
        });
      } else if (srv.last_error) {
        lanBox.innerHTML = `<span style="color:#f87171;">⚠️ 服务错误: ${escapeHtml(srv.last_error)}</span>`;
      } else {
        lanBox.textContent = "开启后将自动显示本机局域网 IP 上报地址与 Web 监控网址（可点「生成朋友矿机一键上报包」发给朋友运行）";
      }
    }
  }

  function resetCollectorDetail(msg = "请先从左侧选择一个客户端节点...") {
    document.getElementById("col-detail-title").textContent = "选择一个客户端查看真实算力与内核输出";
    const badgeEl = document.getElementById("col-detail-badge");
    if (badgeEl) badgeEl.className = "hidden";
    document.getElementById("col-detail-meta").textContent = "等待选择客户端节点";
    document.getElementById("btn-col-delete-client")?.classList.add("hidden");
    document.getElementById("col-detail-metrics")?.classList.add("hidden");
    document.getElementById("col-detail-sparkline")?.classList.add("hidden");
    const tbody = document.getElementById("col-detail-devices-tbody");
    if (tbody) tbody.innerHTML = `<tr><td colspan="7" class="empty-state">暂无设备详情</td></tr>`;
    colDetailLogs = [];
    const term = document.getElementById("col-detail-terminal");
    if (term) term.innerHTML = `<div class="c-muted">${escapeHtml(msg)}</div>`;
  }

  async function loadCollectorClientDetail(clientId) {
    if (!clientId) {
      resetCollectorDetail();
      return;
    }
    try {
      const detail = await api().fetch_collector_client_detail(clientId, 250);
      const c = detail.client || {};
      const logs = Array.isArray(detail.logs) ? detail.logs : [];
      colDetailLogs = logs;

      document.getElementById("col-detail-title").textContent = c.client_name || clientId;
      const badgeEl = document.getElementById("col-detail-badge");
      if (badgeEl) {
        badgeEl.textContent = c.is_online ? "在线上报中" : "已超时离线";
        badgeEl.className = `status-badge ${c.is_online ? "status-online" : "status-offline"}`;
      }
      document.getElementById("col-detail-meta").textContent =
        `主机: ${c.host_name || "--"} · IP: ${c.client_ip || "--"} · 矿工名: ${c.worker_name || "--"} · 算法: ${c.algorithm || "--"} · 矿池: ${c.pool_url || "--"}`;

      document.getElementById("btn-col-delete-client")?.classList.remove("hidden");

      const metricsEl = document.getElementById("col-detail-metrics");
      if (metricsEl) {
        const pills = [
          `<div class="col-metric-pill">实时算力: <strong class="c-pri mono">${escapeHtml(c.current_hashrate_text || "0.00 TH/s")}</strong></div>`,
          `<div class="col-metric-pill">会话均值: <strong class="mono">${Number(c.session_avg_ths || 0).toFixed(2)} TH/s</strong></div>`,
          `<div class="col-metric-pill">5m / 15m: <strong class="mono">${Number(c.avg_5m_ths || 0).toFixed(2)} / ${Number(c.avg_15m_ths || 0).toFixed(2)} TH/s</strong></div>`,
          `<div class="col-metric-pill">稳定度: <strong class="mono">${Number(c.stability_pct || 0) > 0 ? Number(c.stability_pct).toFixed(1) + "%" : "--"}</strong></div>`,
          `<div class="col-metric-pill">份额 A/R: <strong class="mono">${fmtInt(c.accepted_shares || 0)} / ${fmtInt(c.rejected_shares || 0)}</strong> (${Number(c.accept_rate_pct ?? 100).toFixed(2)}%)</div>`,
          `<div class="col-metric-pill">平均延迟: <strong class="mono">${Number(c.avg_latency_ms || 0) > 0 ? Math.round(c.avg_latency_ms) + "ms" : "--"}</strong></div>`,
          `<div class="col-metric-pill">功耗: <strong class="mono">${Number(c.total_power_w || 0) > 0 ? Math.round(c.total_power_w) + "W" : "--"}</strong></div>`,
          `<div class="col-metric-pill">版本: <span class="mono">${escapeHtml(c.miner_version || "--")}</span></div>`,
        ];
        metricsEl.innerHTML = pills.join("");
        metricsEl.classList.remove("hidden");
      }

      const sparkEl = document.getElementById("col-detail-sparkline");
      if (sparkEl) {
        const samples = Array.isArray(c.extra?.samples) ? c.extra.samples : [];
        sparkEl.innerHTML = buildHashrateSparklineSvg(samples, 0);
        sparkEl.classList.toggle("hidden", samples.length === 0);
      }

      renderDevicesTable(document.getElementById("col-detail-devices-tbody"), c.devices || [], false);
      renderTerminalBox(
        document.getElementById("col-detail-terminal"),
        logs,
        "",
        "该客户端暂未上传控制台日志行...",
        true
      );
    } catch (e) {
      resetCollectorDetail(`加载节点详情失败: ${e.message || e}`);
    }
  }

  async function refreshCollectorClients(silent = false) {
    try {
      const res = await api().fetch_collector_clients();
      if (!res || !res.ok) {
        state.colSummary = null;
        document.getElementById("col-stat-clients").textContent = "0 / 0";
        document.getElementById("col-stat-server-time").textContent = (res && res.error) || "未连接收集后端";
        document.getElementById("col-stat-total-hash").textContent = "0.00 TH/s";
        document.getElementById("col-stat-pool-compare").textContent = "对比矿池首页算力: --";
        document.getElementById("col-stat-shares").textContent = "0";
        document.getElementById("col-stat-accept-rate").textContent = "验收率: --%";
        document.getElementById("col-stat-power").textContent = "0 W";
        document.getElementById("col-stat-devices").textContent = "设备数: 0";
        document.getElementById("col-clients-list").innerHTML = `<div class="empty-state">${escapeHtml((res && res.error) || "尚未启动服务或未连接后端")}</div>`;
        resetCollectorDetail((res && res.error) || "请先启动本机收集后端或配置远程后端 URL");
        return;
      }

      state.colSummary = res;
      document.getElementById("col-stat-clients").textContent = `${res.clients_online || 0} / ${res.clients_total || 0}`;
      document.getElementById("col-stat-server-time").textContent = `数据源: ${res.source === "remote" ? "远程后端" : "本机后端"} · ${res.server_time || ""}`;
      document.getElementById("col-stat-total-hash").textContent = res.total_real_hashrate_text || "0.00 TH/s";

      const ov = (state.data && state.data.active_wallet && state.data.active_wallet.overview) || {};
      const poolLiveThs = parseHashrateThsText(ov.live_hashrate);
      const colThs = Number(res.total_real_hashrate_ths || 0);
      const cmpEl = document.getElementById("col-stat-pool-compare");
      if (cmpEl) {
        if (poolLiveThs > 0 && colThs > 0) {
          const diff = colThs - poolLiveThs;
          cmpEl.textContent = `矿池实时 ${ov.live_hashrate} · 占比 ${((colThs / poolLiveThs) * 100).toFixed(1)}% (${diff >= 0 ? "+" : ""}${diff.toFixed(2)} TH/s)`;
        } else {
          cmpEl.textContent = `矿池首页算力: ${ov.live_hashrate || "--"}`;
        }
      }

      document.getElementById("col-stat-shares").textContent = fmtInt(res.total_accepted_shares || 0);
      document.getElementById("col-stat-accept-rate").textContent =
        `拒绝 ${fmtInt(res.total_rejected_shares || 0)} · 验收率 ${Number(res.overall_accept_rate_pct ?? 100).toFixed(2)}%`;
      document.getElementById("col-stat-power").textContent =
        Number(res.total_power_w || 0) > 0 ? `${Math.round(res.total_power_w)} W` : "-- W";
      document.getElementById("col-stat-devices").textContent = `设备数: ${res.total_devices || 0}`;

      const clients = Array.isArray(res.clients) ? res.clients : [];
      const listEl = document.getElementById("col-clients-list");
      if (!clients.length) {
        listEl.innerHTML = `<div class="empty-state">后端已就绪，等待矿机客户端上报数据...</div>`;
        state.colSelectedClientId = "";
        resetCollectorDetail("尚无客户端接入，可在本机首页开启同步上报或导出「朋友矿机一键上报包」发给成员运行");
        return;
      }

      if (!clients.some((c) => c.client_id === state.colSelectedClientId)) {
        state.colSelectedClientId = clients[0].client_id;
      }

      listEl.innerHTML = clients
        .map((c) => {
          const active = c.client_id === state.colSelectedClientId ? "active" : "";
          const dotCls = c.is_online ? "online" : "offline";
          return `
            <div class="col-client-item ${active}" data-client-id="${escapeHtml(c.client_id)}">
              <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
                <div style="display:flex;align-items:center;gap:6px;min-width:0;">
                  <span class="dot ${dotCls}"></span>
                  <strong style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(c.client_name || c.client_id)}</strong>
                  <span class="member-tag" style="--mc:${escapeHtml(c.member_color || "#64748b")};font-size:11px;padding:1px 6px;">${escapeHtml(c.member_name || "未分配")}</span>
                </div>
                <span class="mono c-pri" style="font-weight:700;white-space:nowrap;">${escapeHtml(c.current_hashrate_text || "0.00 TH/s")}</span>
              </div>
              <div class="setting-desc" style="margin-top:4px;display:flex;justify-content:space-between;gap:6px;flex-wrap:wrap;">
                <span>矿工: ${escapeHtml(c.worker_name || "--")} · GPU×${c.device_count || 0}</span>
                <span>A:${fmtInt(c.accepted_shares || 0)} / R:${fmtInt(c.rejected_shares || 0)} · ${escapeHtml((c.last_seen_at || "").slice(11, 19) || "--")}</span>
              </div>
            </div>
          `;
        })
        .join("");

      listEl.querySelectorAll(".col-client-item").forEach((card) => {
        card.onclick = () => {
          state.colSelectedClientId = card.dataset.clientId;
          listEl.querySelectorAll(".col-client-item").forEach((el) => {
            el.classList.toggle("active", el.dataset.clientId === state.colSelectedClientId);
          });
          loadCollectorClientDetail(state.colSelectedClientId);
        };
      });

      if (state.colSelectedClientId) {
        await loadCollectorClientDetail(state.colSelectedClientId);
      }
    } catch (e) {
      if (!silent) toast(`获取集群数据失败: ${e.message || e}`, "error");
    }
  }

  // -------------------------------------------------------------------
  // Miner Kernel Updater, GitHub Release Checker & .bat Configurator
  // -------------------------------------------------------------------
  function formatBytesMb(bytes) {
    const b = Number(bytes || 0);
    if (b <= 0) return "0 MB";
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
    return `${(b / 1024 / 1024).toFixed(2)} MB`;
  }

  function renderKernelDirInfo(info) {
    state.kernelInfo = info || null;
    const dirInput = document.getElementById("kernel-dir-input");
    const badge = document.getElementById("kernel-dir-badge");
    const exeEl = document.getElementById("kernel-stat-exe");
    const exeMetaEl = document.getElementById("kernel-stat-exe-meta");
    const verEl = document.getElementById("kernel-stat-ver");
    const verSrcEl = document.getElementById("kernel-stat-ver-src");
    const batsEl = document.getElementById("kernel-stat-bats");
    const procEl = document.getElementById("kernel-stat-proc");
    const batSel = document.getElementById("kernel-bat-select");

    if (!info || !info.kernel_dir) {
      if (badge) {
        badge.textContent = "未绑定目录";
        badge.className = "status-badge";
        badge.style.background = "rgba(255,255,255,0.07)";
        badge.style.color = "var(--text-muted)";
      }
      if (exeEl) exeEl.textContent = "未检测";
      if (exeMetaEl) exeMetaEl.textContent = "请先选择或填写内核目录";
      if (verEl) verEl.textContent = "--";
      if (verSrcEl) verSrcEl.textContent = "尚未绑定内核目录";
      if (batsEl) batsEl.textContent = "0 个";
      if (procEl) procEl.textContent = "无运行中进程锁定";
      if (batSel) batSel.innerHTML = `<option value="">-- 新建脚本 / 未选择 --</option>`;
      return;
    }

    if (dirInput && document.activeElement !== dirInput && !dirInput.value.trim()) {
      dirInput.value = info.kernel_dir;
    }

    if (badge) {
      badge.style.background = "";
      badge.style.color = "";
      if (info.exe_exists) {
        badge.textContent = `已绑定 · ${info.exe_name}`;
        badge.className = "status-badge status-online";
      } else if (info.exists) {
        badge.textContent = "目录已就绪 (暂无 EXE，可新机全量部署)";
        badge.className = "status-badge status-pending";
      } else {
        badge.textContent = "新目录 (完整部署将自动创建)";
        badge.className = "status-badge status-pending";
      }
    }

    if (exeEl) exeEl.textContent = info.exe_exists ? info.exe_name : "未发现 EXE";
    if (exeMetaEl) {
      exeMetaEl.textContent = info.exe_exists
        ? `${formatBytesMb(info.exe_size)} · 修改于 ${info.exe_modified_at || "--"}`
        : info.exists
          ? "适合点击「新电脑完整部署内核」"
          : "目录尚未在磁盘创建";
    }
    if (verEl) verEl.textContent = info.exe_version || (info.exe_exists ? "未知版本" : "未安装");
    if (verSrcEl) {
      const srcMap = {
        meta: "来源：本工具升级记录 (.pearl_kernel_meta.json)",
        pe: "来源：EXE PE 文件版本信息",
        meta_stale: "来源：历史升级记录（文件大小有变化）",
        folder_name: "来源：内核目录名识别",
      };
      verSrcEl.textContent = srcMap[info.version_source] || (info.exe_exists ? "点击上方 GitHub 检查可比对并写入版本号" : "部署后自动记录版本");
    }

    const batList = info.bat_files || [];
    if (batsEl) batsEl.textContent = `${batList.length} 个`;
    const procs = info.running_processes || [];
    if (procEl) {
      if (procs.length) {
        procEl.textContent = `⚠ 运行中 PID ${procs.map((p) => p.pid).join(", ")} (覆盖更新前将先停止)`;
        procEl.style.color = "var(--warning)";
      } else {
        procEl.textContent = "当前无该目录运行中的进程锁定";
        procEl.style.color = "";
      }
    }

    if (batSel) {
      const prevVal = batSel.value;
      const opts = ['<option value="">-- 新建脚本 / 选择目录内 .bat --</option>'].concat(
        batList.map(
          (b) =>
            `<option value="${escapeHtml(b.path)}"${b.is_linked ? " data-linked='1'" : ""}>${escapeHtml(b.name)}${b.is_linked ? " [已关联]" : ""}</option>`
        )
      );
      batSel.innerHTML = opts.join("");
      if (prevVal && batList.some((b) => b.path === prevVal)) {
        batSel.value = prevVal;
      } else {
        const linked = batList.find((b) => b.is_linked) || batList[0];
        if (linked && !state.batLoadedOnce) {
          batSel.value = linked.path;
        }
      }
    }
  }

  function renderSelectedReleaseDetail() {
    const sel = document.getElementById("kernel-release-select");
    const assetEl = document.getElementById("kernel-asset-info");
    const metaEl = document.getElementById("kernel-release-meta");
    const bodyEl = document.getElementById("kernel-release-body");
    const openBtn = document.getElementById("btn-kernel-open-release-url");

    const idx = Number(sel ? sel.value : 0);
    const rel = (state.kernelReleases || [])[idx];
    if (!rel) return;

    state.kernelLatestHtmlUrl = rel.html_url || "";
    if (openBtn) openBtn.classList.toggle("hidden", !rel.html_url);

    const asset = rel.asset;
    if (assetEl) {
      assetEl.value = asset
        ? `${asset.name} (${asset.size ? formatBytesMb(asset.size) : "大小未知"})`
        : "该版本未匹配到 Windows (.zip) 安装包";
    }
    if (metaEl) {
      metaEl.textContent = `${rel.name || rel.tag_name} · 发布于 ${rel.published_at || "--"}${rel.prerelease ? " [Pre-release]" : ""}`;
    }
    if (bodyEl) {
      bodyEl.textContent = rel.body || "（该版本未填写更新说明）";
    }

    const hasAsset = Boolean(asset && asset.download_url);
    const btnExe = document.getElementById("btn-kernel-update-exe-only");
    const btnFull = document.getElementById("btn-kernel-full-deploy");
    if (btnExe) btnExe.disabled = !hasAsset;
    if (btnFull) btnFull.disabled = !hasAsset;
  }

  function renderKernelUpdateStatusBadge(updateStatus, localVer, latestVer) {
    const badge = document.getElementById("kernel-update-status-badge");
    if (!badge) return;
    badge.style.background = "";
    badge.style.color = "";
    if (updateStatus === "upgrade") {
      badge.textContent = `发现新版本 ${latestVer}（本地 ${localVer}）`;
      badge.className = "status-badge status-pending";
    } else if (updateStatus === "latest") {
      badge.textContent = `已是最新 (${latestVer})`;
      badge.className = "status-badge status-online";
    } else if (updateStatus === "ahead") {
      badge.textContent = `本地版本较新 (${localVer})`;
      badge.className = "status-badge status-online";
    } else if (updateStatus === "not_installed") {
      badge.textContent = `新机待部署 · 最新 ${latestVer}`;
      badge.className = "status-badge status-pending";
    } else {
      badge.textContent = latestVer ? `最新线上版本 ${latestVer}` : "已拉取";
      badge.className = "status-badge status-missing";
    }
  }

  function collectBatFormConfig() {
    return {
      executable: (document.getElementById("bat-executable")?.value || "").trim() || "%~dp0SRBMiner-MULTI.exe",
      algorithm: (document.getElementById("bat-algorithm")?.value || "").trim(),
      pool: (document.getElementById("bat-pool")?.value || "").trim(),
      wallet: (document.getElementById("bat-wallet")?.value || "").trim(),
      worker: (document.getElementById("bat-worker")?.value || "").trim(),
      password: (document.getElementById("bat-password")?.value || "").trim(),
      log_file: (document.getElementById("bat-log-file")?.value || "").trim(),
      api_enable: Boolean(document.getElementById("bat-api-enable")?.checked),
      api_port: (document.getElementById("bat-api-port")?.value || "21550").trim(),
      disable_cpu: Boolean(document.getElementById("bat-disable-cpu")?.checked),
      has_loop: Boolean(document.getElementById("bat-has-loop")?.checked),
      has_pause: Boolean(document.getElementById("bat-has-pause")?.checked),
      extra_args: (document.getElementById("bat-extra-args")?.value || "").trim(),
      preamble: document.getElementById("bat-preamble")?.value || "",
    };
  }

  async function refreshBatPreview(force = false) {
    const manualRaw = Boolean(document.getElementById("bat-manual-raw")?.checked);
    if (manualRaw && !force) return;
    if (force && document.getElementById("bat-manual-raw")) {
      document.getElementById("bat-manual-raw").checked = false;
    }
    try {
      const res = await api().preview_kernel_bat(collectBatFormConfig());
      const box = document.getElementById("bat-raw-preview");
      if (box) box.value = res.content || "";
    } catch (e) {
      // Ignore transient validation error while typing
    }
  }

  function applyBatConfigToForm(cfg) {
    if (!cfg) return;
    state.batLoadedOnce = true;
    const setVal = (id, v) => {
      const el = document.getElementById(id);
      if (el) el.value = v ?? "";
    };
    const setChk = (id, v) => {
      const el = document.getElementById(id);
      if (el) el.checked = Boolean(v);
    };
    setVal("bat-target-path", cfg.bat_path || "start_prl.bat");
    setVal("bat-executable", cfg.executable || "%~dp0SRBMiner-MULTI.exe");
    setVal("bat-algorithm", cfg.algorithm || "pearlhash");
    setVal("bat-pool", cfg.pool || "");
    setVal("bat-wallet", cfg.wallet || "");
    setVal("bat-worker", cfg.worker || "");
    setVal("bat-password", cfg.password || "");
    setVal("bat-log-file", cfg.log_file || "");
    setVal("bat-api-port", cfg.api_port || "21550");
    setVal("bat-extra-args", cfg.extra_args || "");
    setVal("bat-preamble", cfg.preamble || "");
    setChk("bat-disable-cpu", cfg.disable_cpu);
    setChk("bat-api-enable", cfg.api_enable);
    setChk("bat-has-loop", cfg.has_loop);
    setChk("bat-has-pause", cfg.has_pause);
    setChk("bat-manual-raw", false);
    if (cfg.raw_content != null) {
      setVal("bat-raw-preview", cfg.raw_content);
    } else {
      refreshBatPreview(true);
    }
  }

  async function loadKernelBatByPath(batPath) {
    if (!batPath) return;
    try {
      const cfg = await api().load_kernel_bat(batPath);
      applyBatConfigToForm(cfg);
      toast(`已载入脚本配置: ${cfg.bat_name}`);
    } catch (e) {
      toast(`读取 .bat 失败: ${e.message || e}`, "error");
    }
  }

  async function loadKernelView(dirOverride = "", saveSetting = false) {
    try {
      const res = await api().inspect_kernel_dir(dirOverride, saveSetting);
      const settings = res.settings || (state.data && state.data.settings) || {};
      const dirInput = document.getElementById("kernel-dir-input");
      if (dirOverride && dirInput) {
        dirInput.value = dirOverride;
      } else if (dirInput && !dirInput.value.trim() && res.info?.kernel_dir) {
        dirInput.value = res.info.kernel_dir;
      }
      const repoEl = document.getElementById("kernel-github-repo");
      if (repoEl && !repoEl.value.trim() && settings.miner_github_repo) {
        repoEl.value = settings.miner_github_repo;
      }
      const proxyEl = document.getElementById("kernel-github-proxy");
      if (proxyEl && !proxyEl.dataset.inited) {
        proxyEl.value = settings.miner_github_proxy || "";
        proxyEl.dataset.inited = "1";
      }

      renderKernelDirInfo(res.info);

      if (!state.batLoadedOnce) {
        const linkedBat = settings.miner_kernel_bat_path || settings.local_bat_path || "";
        const foundMatch = (res.info?.bat_files || []).find((b) => b.path === linkedBat) || (res.info?.bat_files || [])[0];
        if (foundMatch) {
          await loadKernelBatByPath(foundMatch.path);
        } else {
          const wEl = document.getElementById("bat-wallet");
          if (wEl && !wEl.value.trim() && state.data?.active_wallet?.address) {
            wEl.value = state.data.active_wallet.address;
          }
          await refreshBatPreview(true);
        }
      }

      if (res.task && res.task.running) {
        startKernelProgressPolling();
      }
    } catch (e) {
      toast(`检测内核目录失败: ${e.message || e}`, "error");
    }
  }

  async function checkKernelUpdates() {
    const repo = (document.getElementById("kernel-github-repo")?.value || "").trim();
    const proxy = (document.getElementById("kernel-github-proxy")?.value || "").trim();
    const dir = (document.getElementById("kernel-dir-input")?.value || "").trim();
    const btn = document.getElementById("btn-kernel-check-updates");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "⏳ 正在拉取 GitHub...";
    }
    try {
      const res = await api().check_kernel_updates(repo, proxy, dir);
      state.kernelReleases = res.releases || [];
      if (res.kernel_info) renderKernelDirInfo(res.kernel_info);
      const sel = document.getElementById("kernel-release-select");
      if (sel) {
        sel.innerHTML = state.kernelReleases
          .map((r, i) => `<option value="${i}">${escapeHtml(r.tag_name || r.version)}${i === 0 ? " (最新)" : ""} · ${escapeHtml(r.published_at || "")}</option>`)
          .join("");
        sel.value = "0";
      }
      document.getElementById("kernel-release-box")?.classList.remove("hidden");
      renderSelectedReleaseDetail();
      renderKernelUpdateStatusBadge(res.update_status, res.local_version || "--", res.latest?.version || "--");
      toast(`已获取 ${state.kernelReleases.length} 个 GitHub 发行版`);
    } catch (e) {
      toast(`检查更新失败: ${e.message || e}`, "error");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "🔄 拉取 GitHub 检查更新";
      }
    }
  }

  function renderKernelTaskProgress(task) {
    const wrap = document.getElementById("kernel-progress-wrap");
    const txtEl = document.getElementById("kernel-progress-text");
    const metEl = document.getElementById("kernel-progress-metrics");
    const barEl = document.getElementById("kernel-progress-bar");
    if (!wrap) return;
    if (!task || task.phase === "idle") {
      wrap.classList.add("hidden");
      return;
    }
    wrap.classList.remove("hidden");
    if (txtEl) txtEl.textContent = task.message || "处理中...";
    if (barEl) barEl.style.width = `${Math.max(0, Math.min(100, Number(task.percent || 0)))}%`;
    if (metEl) {
      if (task.total_bytes > 0 && task.phase === "downloading") {
        metEl.textContent = `${Number(task.percent || 0).toFixed(1)}% · ${formatBytesMb(task.downloaded_bytes)} / ${formatBytesMb(task.total_bytes)} · ${Math.round(task.speed_kbps || 0)} KB/s`;
      } else if (task.phase === "downloading" && task.downloaded_bytes > 0) {
        metEl.textContent = `${formatBytesMb(task.downloaded_bytes)} · ${Math.round(task.speed_kbps || 0)} KB/s`;
      } else {
        metEl.textContent = `${Math.round(task.percent || 0)}%`;
      }
    }
  }

  function startKernelProgressPolling() {
    if (state.kernelTaskTimer) clearInterval(state.kernelTaskTimer);
    const tick = async () => {
      try {
        const task = await api().get_kernel_task_status();
        renderKernelTaskProgress(task);
        if (!task.running) {
          clearInterval(state.kernelTaskTimer);
          state.kernelTaskTimer = null;
          if (task.phase === "done") {
            toast(task.message || "内核部署/更新完成");
            const kInfo = task.result?.kernel_info;
            if (kInfo) renderKernelDirInfo(kInfo);
            const latestRel = state.kernelReleases[0];
            if (latestRel && task.result?.version) {
              renderKernelUpdateStatusBadge("latest", task.result.version, latestRel.version);
            }
          } else if (task.phase === "error") {
            toast(task.error || "更新内核失败", "error");
          }
        }
      } catch (e) {
        clearInterval(state.kernelTaskTimer);
        state.kernelTaskTimer = null;
      }
    };
    tick();
    state.kernelTaskTimer = setInterval(tick, 400);
  }

  async function triggerKernelDeploy(mode) {
    const dir = (document.getElementById("kernel-dir-input")?.value || "").trim();
    if (!dir) return toast("请先选择或填写矿机内核目录路径", "warn");
    const idx = Number(document.getElementById("kernel-release-select")?.value || 0);
    const rel = (state.kernelReleases || [])[idx];
    if (!rel || !rel.asset || !rel.asset.download_url) {
      return toast("请先拉取 GitHub 更新并选择含 Windows 压缩包的发行版", "warn");
    }

    const stopProc = Boolean(document.getElementById("kernel-opt-stop-proc")?.checked);
    const backupExe = Boolean(document.getElementById("kernel-opt-backup-exe")?.checked);
    const modeTitle = mode === "exe_only" ? "仅提取覆盖 SRBMiner-MULTI.exe" : "新电脑完整部署内核";
    const procs = state.kernelInfo?.running_processes || [];
    const extraWarn = procs.length && stopProc ? `\n\n注意：将先自动停止当前运行中的 PID ${procs.map((p) => p.pid).join(", ")} 以解除文件占用。` : "";

    const ok = await confirmDialog(
      `确认执行【${modeTitle}】？\n\n目标目录：${dir}\n下载包：${rel.asset.name}\n操作完成后将自动删除下载的压缩包。${extraWarn}`,
      { title: modeTitle, confirmText: "开始执行" }
    );
    if (!ok) return;

    try {
      const task = await api().start_kernel_deploy({
        kernel_dir: dir,
        repo: (document.getElementById("kernel-github-repo")?.value || "").trim(),
        github_proxy: (document.getElementById("kernel-github-proxy")?.value || "").trim(),
        download_url: rel.asset.download_url,
        asset_name: rel.asset.name,
        tag_name: rel.tag_name,
        digest: rel.asset.digest || "",
        mode,
        stop_running: stopProc,
        backup_exe: backupExe,
      });
      renderKernelTaskProgress(task);
      startKernelProgressPolling();
    } catch (e) {
      toast(`启动部署任务失败: ${e.message || e}`, "error");
    }
  }

  async function saveKernelBatAction({ bindAsHomeBat = false, startNow = false } = {}) {
    const targetPath = (document.getElementById("bat-target-path")?.value || "").trim() || "start_prl.bat";
    const kernelDir = (document.getElementById("kernel-dir-input")?.value || "").trim();
    const useRaw = Boolean(document.getElementById("bat-manual-raw")?.checked);
    const rawContent = document.getElementById("bat-raw-preview")?.value || "";
    const backupExisting = Boolean(document.getElementById("bat-backup-existing")?.checked);

    try {
      const res = await api().save_kernel_bat({
        target_path: targetPath,
        kernel_dir: kernelDir,
        config: collectBatFormConfig(),
        raw_content: rawContent,
        use_raw: useRaw,
        backup_existing: backupExisting,
        bind_as_home_bat: bindAsHomeBat || startNow,
        start_now: startNow,
      });
      if (res.kernel_info) {
        const dirEl = document.getElementById("kernel-dir-input");
        if (dirEl && !dirEl.value.trim()) dirEl.value = res.kernel_info.kernel_dir;
        renderKernelDirInfo(res.kernel_info);
      }
      if (res.config) applyBatConfigToForm(res.config);
      if (bindAsHomeBat || startNow) {
        await loadState();
      }
      toast(startNow ? `已保存并启动矿机脚本: ${res.saved_path}` : `脚本已保存至: ${res.saved_path}`);
      if (startNow) {
        await switchView("dashboard");
        await pollLocalMinerStatus(true);
      }
    } catch (e) {
      toast(`保存 .bat 失败: ${e.message || e}`, "error");
    }
  }

  // -------------------------------------------------------------------
  // Navigation & Action Wiring
  // -------------------------------------------------------------------
  async function switchView(viewName) {
    state.currentView = viewName;
    document.querySelectorAll(".nav-item").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === viewName);
    });
    document.querySelectorAll(".view").forEach((v) => {
      v.classList.toggle("active", v.id === `view-${viewName}`);
    });
    if (viewName === "settle") {
      if (state.activeDraftId && state.settleDraft) {
        renderSettleDraftsPanel();
        await recomputeSettlePreview();
      } else {
        await initSettleDraft();
      }
    } else if (viewName === "collector") {
      await refreshCollectorClients(true);
    } else if (viewName === "kernel") {
      await loadKernelView();
    } else if (viewName === "alerts") {
      renderAlertsView({ force: true });
      if (state.data?.alert_config?.standby_sync_enabled) {
        api()
          .sync_standby_now()
          .then((res) => {
            if (res && res.state) applyStatePayload(res.state);
          })
          .catch(() => {});
      }
    }
  }

  function bindEvents() {
    document.getElementById("modal-close").onclick = closeModal;
    document.getElementById("modal-backdrop").onclick = (e) => {
      if (e.target.id === "modal-backdrop") closeModal();
    };
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        const confirmBd = document.getElementById("confirm-backdrop");
        if (confirmBd && !confirmBd.classList.contains("hidden")) {
          document.getElementById("confirm-cancel")?.click();
          return;
        }
        if (state.notifDrawerOpen) {
          closeNotifDrawer();
          return;
        }
        const modalBd = document.getElementById("modal-backdrop");
        if (modalBd && !modalBd.classList.contains("hidden")) {
          closeModal();
        }
      }
    });

    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.onclick = () => switchView(btn.dataset.view);
    });

    document.getElementById("btn-add-wallet").onclick = openAddWalletModal;
    document.getElementById("btn-edit-active-wallet").onclick = () => {
      if (state.data?.active_wallet) openEditWalletModal(state.data.active_wallet);
    };
    document.getElementById("btn-open-pool-url").onclick = () => {
      const addr = state.data?.active_wallet?.address;
      if (addr) api().open_url(`https://www.ntminerpool.com/prl/index?back=${addr}`);
    };
    document.getElementById("btn-copy-address").onclick = () => {
      const addr = state.data?.active_wallet?.address;
      if (addr) copyText(addr, "已复制钱包地址");
    };

    const handleManualRefresh = async () => {
      const wid = state.data?.active_wallet?.id;
      if (!wid) return;
      const spin = document.getElementById("refresh-spin");
      spin.classList.add("spin");
      try {
        const res = await api().refresh_wallet(wid);
        applyStatePayload(res.state);
        toast("已拉取矿池最新数据并重置同步倒计时");
      } catch (e) {
        try {
          await loadState(wid);
        } catch (_err) {
          // ignore secondary error
        }
        toast(`拉取失败: ${e}`, "error");
      } finally {
        spin.classList.remove("spin");
      }
    };
    document.getElementById("btn-refresh-now").onclick = handleManualRefresh;
    const retryBtn = document.getElementById("btn-retry-pool-conn");
    if (retryBtn) retryBtn.onclick = handleManualRefresh;

    const toggleWatch = async () => {
      const wid = state.data?.active_wallet?.id;
      if (!wid) return;
      const isWatching = state.data.poller.watch_mode && state.data.poller.watch_wallet_id === wid;
      await api().set_watch_mode(!isWatching, !isWatching ? wid : null);
      await loadState(wid);
      toast(!isWatching ? "已开启离线矿机唤醒监听 (每8秒)" : "已停止唤醒监听");
    };
    document.getElementById("btn-toggle-watch").onclick = toggleWatch;
    document.getElementById("btn-stop-watch-banner").onclick = toggleWatch;

    document.getElementById("btn-assign-first-unknown").onclick = () => {
      const first = state.data?.unassigned_workers?.[0];
      if (first) openQuickAssignModal(first.worker_name);
    };

    // Dashboard Filters & Actions
    document.querySelectorAll("#worker-filter-pills .pill").forEach((p) => {
      p.onclick = () => {
        document.querySelectorAll("#worker-filter-pills .pill").forEach((x) => x.classList.remove("active"));
        p.classList.add("active");
        state.workerFilter = p.dataset.filter;
        renderDashboard();
      };
    });
    document.getElementById("worker-search-input").oninput = (e) => {
      state.workerSearch = e.target.value;
      renderDashboard();
    };
    document.getElementById("btn-add-manual-worker").onclick = () => openManualWorkerModal();
    document.getElementById("btn-go-settle").onclick = () => switchView("settle");
    document.getElementById("btn-dash-sync").onclick = () => openSyncRecordsModal("export", null);
    document.getElementById("btn-dash-calibrate-paid").onclick = () => openCalibratePoolPaidModal(null);
    document.getElementById("btn-dash-import-baseline").onclick = () => openHistoricalBaselineModal(null);
    document.getElementById("btn-quick-baseline").onclick = async () => {
      if (!state.data?.active_wallet) return;
      const ok = await confirmDialog(
        "将当前所有在线及已缓存矿机的实时累计份额保存为「最新基准起点」吗？（不会产生人民币分红记录）",
        { title: "保存当前份额为基准起点", confirmText: "保存基准" }
      );
      if (!ok) return;
      const draft = await api().preview_settlement(state.data.active_wallet.id, null);
      const res = await api().save_checkpoint_and_settle({
        wallet_id: state.data.active_wallet.id,
        kind: "baseline",
        checkpoint_time: nowLocalStr(),
        label: "快速基准快照",
        prl_amount: 0,
        cny_amount: 0,
        tx_hash: "",
        notes: "",
        prev_checkpoint_id: null,
        worker_entries: draft.workers,
      });
      state.settleDraft = null;
      state.settlePrevCheckpointId = res.saved.id;
      applyStatePayload(res.state);
      toast("已保存当前份额为新的基准起点！");
    };

    // Settle View Events
    document.getElementById("btn-import-historical-baseline").onclick = () => openHistoricalBaselineModal(null);
    document.getElementById("btn-ledger-import-baseline").onclick = () => openHistoricalBaselineModal(null);
    document.getElementById("btn-ledger-export-sync").onclick = () => openSyncRecordsModal("export", null);
    document.getElementById("btn-ledger-import-sync").onclick = () => openSyncRecordsModal("import", null);
    document.getElementById("settle-prev-checkpoint").onchange = async (e) => {
      const nextVal = e.target.value ? Number(e.target.value) : "";
      if (state.activeDraftId) {
        const ok = await confirmDialog(
          "更换上次基准起点会按新基准重载矿机份额并退出草稿编辑模式，是否继续？（已保存草稿仍保留）",
          { title: "更换基准起点确认", confirmText: "确定更换" }
        );
        if (!ok) {
          e.target.value = state.settlePrevCheckpointId ? String(state.settlePrevCheckpointId) : "";
          return;
        }
        state.activeDraftId = null;
        state.activeDraftMeta = null;
      }
      state.settlePrevCheckpointId = nextVal;
      await initSettleDraft();
    };
    document.getElementById("btn-reload-settle-draft").onclick = async () => {
      if (state.activeDraftId) {
        const ok = await confirmDialog(
          "重新载入矿池实时矿机份额将退出当前草稿编辑（已保存的草稿不会丢失），确定继续？",
          { title: "重载实时份额确认", confirmText: "继续重载" }
        );
        if (!ok) return;
        state.activeDraftId = null;
        state.activeDraftMeta = null;
      }
      await initSettleDraft();
      toast("已重新载入最新矿机份额");
    };
    document.getElementById("btn-settle-now").onclick = () => {
      document.getElementById("settle-time").value = nowLocalStr();
      toast("已填入当前时间");
    };
    document.getElementById("btn-open-merge-payments").onclick = openMergePaymentsModal;
    document.getElementById("btn-estimate-cny").onclick = async () => {
      const ov = (state.data?.active_wallet && state.data.active_wallet.overview) || {};
      const price = Number(ov.prl_price_cny || 0);
      if (price <= 0) {
        toast("当前未获取到 PRL 参考币价，请先拉取矿池数据或手动填写金额", "warn");
        return;
      }
      let prl = Number(document.getElementById("settle-prl").value || 0);
      if (prl <= 0) {
        prl = Number(ov.unpaid_prl || 0) + Number(ov.immature_prl || 0);
        if (prl > 0) {
          document.getElementById("settle-prl").value = Number(prl.toFixed(6));
        }
      }
      if (prl <= 0) {
        toast("请先填写本次提现 PRL 数量或从矿池打款记录带入", "warn");
        return;
      }
      const estCny = (prl * price).toFixed(2);
      document.getElementById("settle-cny").value = estCny;
      await recomputeSettlePreview();
      toast(`已按参考价 ￥${price.toFixed(2)}/PRL 填入估算金额 ￥${estCny}`);
    };
    document.querySelectorAll("#settle-worker-filter-pills .pill").forEach((p) => {
      p.onclick = async () => {
        document
          .querySelectorAll("#settle-worker-filter-pills .pill")
          .forEach((x) => x.classList.remove("active"));
        p.classList.add("active");
        state.settleWorkerFilter = p.dataset.sfilter;
        await recomputeSettlePreview();
      };
    });
    document.getElementById("settle-cny").oninput = () => recomputeSettlePreview();
    document.getElementById("settle-prl").oninput = () => recomputeSettlePreview();

    document.getElementById("btn-settle-add-worker").onclick = () => {
      openModal(
        "临时添加离线矿机到本次结算",
        `
          <div class="form-field">
            <label>矿机名称 <span class="req">*</span></label>
            <input id="saw-name" class="input" placeholder="例如：charlie_01" />
          </div>
          <div class="form-field">
            <label>上次份额 (新矿机填 0)</label>
            <input id="saw-prev" type="number" min="0" class="input" value="0" />
          </div>
          <div class="form-field">
            <label>本次累计份额 <span class="req">*</span></label>
            <input id="saw-curr" type="number" min="0" class="input" value="0" />
          </div>
        `,
        [
          { text: "取消", onClick: closeModal },
          {
            text: "添加到结算表",
            cls: "btn-primary",
            onClick: async () => {
              const name = document.getElementById("saw-name").value.trim();
              if (!name) return toast("请输入矿机名", "error");
              const prev = Number(document.getElementById("saw-prev").value || 0);
              const curr = Number(document.getElementById("saw-curr").value || 0);
              state.settleDraft.workers.push({
                worker_name: name,
                member_id: null,
                member_name: "未分配",
                member_color: "#64748b",
                prev_shares: prev,
                curr_shares: curr,
                share_source: "manual",
              });
              closeModal();
              await recomputeSettlePreview();
              toast("已加入结算表，别忘了确认成员归属");
            },
          },
        ]
      );
    };

    document.getElementById("btn-copy-settle-bill").onclick = () => {
      if (!state.settleSplit || !state.data?.active_wallet) return;
      const text = buildBillText(
        document.getElementById("settle-label").value || state.data.active_wallet.alias,
        state.data.active_wallet.alias,
        document.getElementById("settle-time").value || nowLocalStr(),
        Number(document.getElementById("settle-cny").value || 0),
        Number(document.getElementById("settle-prl").value || 0),
        state.settleSplit.total_delta_shares,
        state.settleSplit.allocations,
        collectFeeFormPayload(),
        null
      );
      copyText(text, "已复制本次分红账单，可直接发微信群");
    };

    // Custom Fee Pipeline, Conversions & Residual Controls
    document.getElementById("btn-toggle-fees-panel").onclick = () => {
      const body = document.getElementById("settle-fees-body");
      if (!body) return;
      setFeesPanelExpanded(body.classList.contains("hidden"));
    };
    document.getElementById("btn-edit-fee-pipeline").onclick = openFeePipelineEditorModal;
    document.getElementById("btn-edit-fee-pipeline-inline").onclick = openFeePipelineEditorModal;
    document.getElementById("btn-add-adhoc-fee-step").onclick = () => {
      const fs = ensureFeeFormState();
      fs.fee_entries.push({
        step_id: `adhoc_${Date.now()}`,
        label: "临时附加手续费",
        category: "other",
        amount: 0,
        currency: "USDT",
        only_official_wallet: false,
      });
      renderFeeDynamicPanels();
      updateFeeLiveSummary();
    };
    document.getElementById("btn-add-conversion-leg").onclick = () => {
      const fs = ensureFeeFormState();
      fs.conversions.push({
        label: "交易所卖币",
        from_asset: "PRL",
        to_asset: "USDT",
        sold_amount: 0,
        unit_price: 0,
        received_amount: 0,
        market_ref_rate: 0,
      });
      renderFeeDynamicPanels();
      updateFeeLiveSummary();
    };
    document.getElementById("btn-add-fx-rate").onclick = () => {
      const fs = ensureFeeFormState();
      fs.fx_rates_cny.push({ asset: "USDT", rate: 0 });
      renderFeeDynamicPanels();
      updateFeeLiveSummary();
    };
    document.getElementById("btn-add-residual-asset").onclick = () => {
      const raw = window.prompt("请输入要记录残余的币种代码（例如 EUR / USD / HKD）:", "EUR");
      const code = (raw || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 10);
      if (!code) return;
      const fs = ensureFeeFormState();
      if (!(code in fs.residual_deltas)) fs.residual_deltas[code] = 0;
      renderFeeDynamicPanels();
      updateFeeLiveSummary();
    };
    document.getElementById("btn-settle-calibrate-dust").onclick = openCalibrateDustModal;
    document.getElementById("settle-fees-panel").addEventListener("click", (e) => {
      if (e.target.closest && e.target.closest(".js-calibrate-dust-btn")) {
        openCalibrateDustModal();
      }
    });
    document.getElementById("btn-quick-log-trade").onclick = () => openAddExchangeTradeModal();
    document.getElementById("btn-import-unsettled-trades").onclick = openImportUnsettledTradesModal;
    document.getElementById("btn-clear-settle-fees").onclick = () => {
      clearFeeFormPayload();
      toast("已清空本轮手续费与交易填写项");
    };

    // Ledger Exchange Trades Controls
    document.getElementById("btn-calibrate-dust").onclick = openCalibrateDustModal;
    document.getElementById("btn-add-exchange-trade").onclick = () => openAddExchangeTradeModal();
    document.getElementById("btn-merge-trades-settle").onclick = async () => {
      const allTrades = (state.data && state.data.exchange_trades) || [];
      const picked = allTrades.filter((t) => state.selectedTradeIds.has(t.id) && t.linked_checkpoint_id == null);
      if (!picked.length) return toast("请先勾选待结算流水记录", "warn");
      const built = buildFeesFromSelectedTrades(picked);
      await switchView("settle");
      await initSettleDraft({
        prl: built.prlTotal > 0 ? built.prlTotal : null,
        label: `合并 ${picked.length} 笔卖币流水结算`,
        fees: built.fees,
      });
      toast(`已合并 ${picked.length} 笔流水到结算页，请填入最终实收人民币`);
    };

    // Onboarding & Empty-Wallet CTA Controls
    document.getElementById("btn-empty-open-wizard").onclick = openOnboardingModal;
    document.getElementById("btn-empty-import-sync").onclick = () => openSyncRecordsModal("import", null);
    document.getElementById("btn-ob-import-sync").onclick = () => openSyncRecordsModal("import", null);
    document.getElementById("btn-ob-add-member").onclick = () => {
      state.onboardingMembers.push({ name: "", prefixes: "", color: "#f59e0b" });
      renderOnboardingMembers();
    };
    document.getElementById("btn-ob-skip").onclick = () => {
      state.onboardingSkipped = true;
      document.getElementById("onboarding-overlay")?.classList.add("hidden");
    };
    document.getElementById("btn-ob-submit").onclick = async () => {
      const addr = document.getElementById("ob-wallet-addr")?.value?.trim() || "";
      if (!addr) {
        return toast("请输入奈特矿池 (ntminerpool.com) 钱包地址或链接", "warn");
      }
      const btn = document.getElementById("btn-ob-submit");
      if (btn) btn.disabled = true;
      try {
        const res = await api().complete_onboarding({
          address_or_url: addr,
          alias: document.getElementById("ob-wallet-alias")?.value?.trim() || "主钱包",
          wallet_type: document.getElementById("ob-wallet-type")?.value || "official",
          notes: document.getElementById("ob-wallet-notes")?.value?.trim() || "",
          pipeline_preset: document.getElementById("ob-pipeline-preset")?.value || "simple_direct",
          members: (state.onboardingMembers || []).filter((m) => (m.name || "").trim()),
        });
        state.onboardingSkipped = false;
        state.feeFormState = null;
        document.getElementById("onboarding-overlay")?.classList.add("hidden");
        applyStatePayload(res.state);
        toast("已完成初始配置并添加奈特矿池钱包！");
      } catch (e) {
        toast(`配置失败: ${e.message || e}`, "error");
      } finally {
        if (btn) btn.disabled = false;
      }
    };

    const submitCheckpoint = async (kind) => {
      if (!state.settleDraft || !state.data?.active_wallet) return;
      const cny = Number(document.getElementById("settle-cny").value || 0);
      const prl = Number(document.getElementById("settle-prl").value || 0);
      if (kind === "withdrawal" && cny <= 0) {
        const ok = await confirmDialog(
          "当前实际到账人民币为 0，是否仍要记录本次提现快照？（后续可在账本中补填人民币并自动重算）",
          { title: "到账金额为 0 确认", confirmText: "继续保存" }
        );
        if (!ok) {
          return;
        }
      }
      try {
        const res = await api().save_checkpoint_and_settle({
          wallet_id: state.data.active_wallet.id,
          kind,
          checkpoint_time: document.getElementById("settle-time").value || nowLocalStr(),
          label: document.getElementById("settle-label").value || (kind === "baseline" ? "基准快照" : ""),
          prl_amount: prl,
          cny_amount: cny,
          tx_hash: "",
          notes: document.getElementById("settle-notes").value || "",
          prev_checkpoint_id: state.settlePrevCheckpointId || null,
          worker_entries: state.settleDraft.workers,
          fees: kind === "withdrawal" ? collectFeeFormPayload() : null,
          consume_draft_id: kind === "withdrawal" ? (state.activeDraftId || null) : null,
        });
        state.settleDraft = null;
        if (kind === "withdrawal") {
          state.activeDraftId = null;
          state.activeDraftMeta = null;
          clearFeeFormPayload();
          setFeesPanelExpanded(false);
        }
        state.settlePrevCheckpointId = res.saved.id;
        document.getElementById("settle-cny").value = "";
        document.getElementById("settle-prl").value = "";
        document.getElementById("settle-label").value = "";
        document.getElementById("settle-notes").value = "";
        applyStatePayload(res.state);
        await initSettleDraft();
        toast(kind === "baseline" ? "基准起点已存档" : "提现结算已保存，并自动设为下次基准！");
      } catch (e) {
        toast(`保存失败: ${e}`, "error");
      }
    };

    const handleSaveDraftClick = async () => {
      if (state.activeDraftId) {
        await saveDraftDirect(false);
      } else {
        openSaveDraftModal(false);
      }
    };
    document.getElementById("btn-save-settle-draft").onclick = handleSaveDraftClick;
    document.getElementById("btn-save-settle-draft-2").onclick = handleSaveDraftClick;
    document.getElementById("btn-confirm-settle").onclick = () => submitCheckpoint("withdrawal");
    document.getElementById("btn-save-baseline-only").onclick = () => submitCheckpoint("baseline");

    // Ledger Exports
    document.getElementById("btn-export-csv-wallet").onclick = async () => {
      const res = await api().export_settlements_csv(state.data?.active_wallet?.id || null);
      if (!res.cancelled) toast(`已导出 CSV 至: ${res.path}`);
    };
    document.getElementById("btn-export-csv-all").onclick = async () => {
      const res = await api().export_settlements_csv(null);
      if (!res.cancelled) toast(`已导出全部 CSV 至: ${res.path}`);
    };

    // Members
    document.getElementById("btn-add-member").onclick = () => openMemberModal(null);

    // Settings
    const saveSettings = async () => {
      await api().update_settings({
        auto_poll_enabled: document.getElementById("set-auto-poll").checked ? "1" : "0",
        auto_poll_interval_sec: document.getElementById("set-poll-interval").value,
        minimize_to_tray: document.getElementById("set-minimize-tray").checked ? "1" : "0",
      });
      await loadState();
      toast("设置已保存");
    };
    document.getElementById("set-auto-poll").onchange = saveSettings;
    document.getElementById("set-poll-interval").onchange = saveSettings;
    document.getElementById("set-minimize-tray").onchange = saveSettings;
    document.getElementById("btn-open-data-dir").onclick = () => api().open_data_dir();
    document.getElementById("btn-backup-json").onclick = async () => {
      const res = await api().export_backup_json();
      toast(`已导出 JSON 备份: ${res.path}`);
    };
    document.getElementById("btn-settings-open-dust-modal").onclick = openCalibrateDustModal;
    document.getElementById("btn-save-settings-dust").onclick = async () => {
      try {
        const pVal = Number(document.getElementById("set-dust-prl").value || 0);
        const uVal = Number(document.getElementById("set-dust-usdt").value || 0);
        if (!Number.isFinite(pVal) || pVal < 0 || !Number.isFinite(uVal) || uVal < 0) {
          return toast("请输入大于等于 0 的有效残余数值", "warn");
        }
        const res = await api().calibrate_residual_balance(
          { PRL: pVal, USDT: uVal },
          state.data?.active_wallet?.id || null
        );
        applyStatePayload(res.state);
        toast(`已保存当前残余：PRL ${fmtPrl(pVal)} · USDT ${fmtPrl(uVal)}`);
      } catch (e) {
        toast(`保存残余失败: ${e.message || e}`, "error");
      }
    };

    // Homepage bottom block rewards toggle
    document.getElementById("btn-toggle-all-rewards").onclick = () => {
      state.showAllRewards = !state.showAllRewards;
      renderBlockRewards();
    };

    // Homepage local SRBMiner binder controls
    document.getElementById("btn-goto-collector").onclick = () => switchView("collector");
    document.getElementById("toggle-local-miner-panel").onchange = async () => {
      const show = document.getElementById("toggle-local-miner-panel").checked;
      document.getElementById("local-miner-body").classList.toggle("hidden", !show);
      await api().update_settings({ show_local_miner_panel: show ? "1" : "0" });
      if (state.data && state.data.settings) {
        state.data.settings.show_local_miner_panel = show ? "1" : "0";
      }
      if (show) {
        const bp = document.getElementById("lm-bat-path").value.trim();
        if (bp) await inspectBatPreview(bp);
        await pollLocalMinerStatus(true);
      }
    };

    document.querySelectorAll("#lm-mode-pills .pill").forEach((btn) => {
      btn.onclick = () => setLocalMinerMode(btn.dataset.lmode);
    });

    document.getElementById("btn-lm-browse-bat").onclick = async () => {
      const res = await api().browse_local_file("bat");
      if (res && !res.cancelled && res.path) {
        document.getElementById("lm-bat-path").value = res.path;
        await api().update_settings({ local_bat_path: res.path });
        await inspectBatPreview(res.path);
      }
    };
    document.getElementById("lm-bat-path").onchange = async () => {
      const val = document.getElementById("lm-bat-path").value.trim();
      await api().update_settings({ local_bat_path: val });
      await inspectBatPreview(val);
    };
    document.getElementById("btn-lm-start-bat").onclick = async () => {
      const batPath = document.getElementById("lm-bat-path").value.trim();
      if (!batPath) {
        toast("请先选择或填写 .bat 启动脚本路径", "warn");
        return;
      }
      const autoInject = document.getElementById("lm-auto-inject-log").checked;
      try {
        state.lmLatestSeq = 0;
        state.lmLines = [];
        const st = await api().bind_bat_start(batPath, autoInject);
        applyLocalBinderActionResult(st);
        if (st.last_error) toast(st.last_error, "error");
        else toast("已启动 .bat 矿机并开始实时监听内核输出");
      } catch (e) {
        toast(`启动失败: ${e.message || e}`, "error");
      }
    };

    const handleStopOrUnbind = async (fn, msg) => {
      try {
        const st = await fn();
        applyLocalBinderActionResult(st);
        toast(msg);
      } catch (e) {
        toast(`操作失败: ${e.message || e}`, "error");
      }
    };
    document.getElementById("btn-lm-stop").onclick = () =>
      handleStopOrUnbind(() => api().stop_bound_miner(), "已停止托管矿机进程");
    document.getElementById("btn-lm-detach").onclick = () =>
      handleStopOrUnbind(() => api().unbind_local_miner(), "已断开进程输出绑定");
    document.getElementById("btn-lm-stop-log").onclick = () =>
      handleStopOrUnbind(() => api().unbind_local_miner(), "已停止追踪日志文件");

    document.getElementById("btn-lm-scan-pids").onclick = scanLocalMinersList;
    document.getElementById("btn-lm-attach-pid").onclick = async () => {
      const pid = document.getElementById("lm-pid-select").value;
      if (!pid) {
        toast("请先扫描并选择一个 SRBMiner 进程", "warn");
        return;
      }
      try {
        state.lmLatestSeq = 0;
        state.lmLines = [];
        const st = await api().bind_running_pid(Number(pid), "");
        applyLocalBinderActionResult(st);
        if (st.last_error) toast(st.last_error, "error");
        else toast(`已挂载运行中进程 PID ${pid}`);
      } catch (e) {
        toast(`挂载失败: ${e.message || e}`, "error");
      }
    };

    document.getElementById("btn-lm-browse-log").onclick = async () => {
      const res = await api().browse_local_file("log");
      if (res && !res.cancelled && res.path) {
        document.getElementById("lm-log-path").value = res.path;
        await api().update_settings({ local_log_path: res.path });
      }
    };
    document.getElementById("lm-log-path").onchange = async () => {
      await api().update_settings({ local_log_path: document.getElementById("lm-log-path").value.trim() });
    };
    document.getElementById("btn-lm-attach-log").onclick = async () => {
      const logPath = document.getElementById("lm-log-path").value.trim();
      if (!logPath) {
        toast("请先选择或填写 SRBMiner 日志文件路径", "warn");
        return;
      }
      try {
        state.lmLatestSeq = 0;
        state.lmLines = [];
        const st = await api().bind_log_file(logPath);
        applyLocalBinderActionResult(st);
        if (st.last_error) toast(st.last_error, "error");
        else toast("已开始实时追踪日志文件");
      } catch (e) {
        toast(`追踪日志失败: ${e.message || e}`, "error");
      }
    };

    const saveTelemetryPrefs = async () => {
      const pushEnabled = document.getElementById("lm-push-enabled").checked ? "1" : "0";
      const clientName = document.getElementById("lm-client-name").value.trim();
      await api().update_settings({
        telemetry_push_enabled: pushEnabled,
        telemetry_client_name: clientName,
      });
      if (state.data && state.data.settings) {
        state.data.settings.telemetry_push_enabled = pushEnabled;
        state.data.settings.telemetry_client_name = clientName;
      }
      await pollLocalMinerStatus(true);
    };
    document.getElementById("lm-push-enabled").onchange = async () => {
      await saveTelemetryPrefs();
      toast(
        document.getElementById("lm-push-enabled").checked
          ? "已开启自动上报到收集服务器"
          : "已关闭自动上报"
      );
    };
    document.getElementById("lm-client-name").onchange = saveTelemetryPrefs;
    document.getElementById("btn-lm-push-now").onclick = async () => {
      await saveTelemetryPrefs();
      const res = await api().push_local_telemetry_now();
      if (res && res.ok) {
        toast("已成功上报本机输出与算力到后端");
      } else {
        toast(`上报失败: ${(res && res.error) || "请确认收集服务器已启动"}`, "error");
      }
      await pollLocalMinerStatus(true);
    };

    const redrawLmTerminal = () => {
      renderTerminalBox(
        document.getElementById("lm-terminal"),
        state.lmLines,
        document.getElementById("lm-log-filter")?.value || "",
        "暂无输出，请绑定并启动 .bat 或挂载运行中的 SRBMiner...",
        document.getElementById("lm-autoscroll")?.checked !== false
      );
    };
    document.getElementById("lm-log-filter").oninput = redrawLmTerminal;
    document.getElementById("lm-autoscroll").onchange = redrawLmTerminal;
    document.getElementById("btn-lm-send-s").onclick = async () => {
      const res = await api().send_local_miner_stats_key();
      toast(
        res && res.sent
          ? "已发送按键 s 触发算力统计输出"
          : "当前模式暂不支持交互按键（建议托管模式或等待自动周期输出）",
        res && res.sent ? "ok" : "warn"
      );
    };
    document.getElementById("btn-lm-copy-logs").onclick = () => {
      if (!state.lmLines.length) return toast("暂无日志可复制", "warn");
      copyText(state.lmLines.map((x) => x.line).join("\n"), "已复制终端日志");
    };
    document.getElementById("btn-lm-clear-logs").onclick = async () => {
      state.lmLines = [];
      state.lmLatestSeq = 0;
      const st = await api().clear_local_miner_logs();
      applyLocalBinderActionResult(st);
      toast("已清空输出缓存");
    };

    // Collector Server & Distributed Cluster Controls
    document.getElementById("btn-col-start-local").onclick = async () => {
      const port = Number(document.getElementById("col-local-port").value || 8765);
      const token = document.getElementById("col-local-token").value.trim();
      try {
        await api().start_local_collector("0.0.0.0", port, token);
        await loadState();
        await refreshCollectorClients(true);
        toast(`本机收集后端服务已在端口 ${port} 启动`);
      } catch (e) {
        toast(`启动收集后端失败: ${e.message || e}`, "error");
      }
    };
    document.getElementById("btn-col-stop-local").onclick = async () => {
      await api().stop_local_collector();
      await loadState();
      await refreshCollectorClients(true);
      toast("已停止本机收集后端服务");
    };
    document.getElementById("btn-col-open-web").onclick = () => {
      const srv = (state.data && state.data.collector_server) || {};
      const port = Number(document.getElementById("col-local-port").value || srv.port || 8765);
      api().open_url(srv.local_url || `http://127.0.0.1:${port}/`);
    };
    document.getElementById("btn-col-save-remote").onclick = async () => {
      const mode = document.getElementById("col-source-mode").value;
      const url = document.getElementById("col-remote-url").value.trim();
      const token = document.getElementById("col-remote-token").value.trim();
      await api().update_settings({
        collector_mode: mode,
        collector_server_url: url,
        collector_token: token,
      });
      await loadState();
      await refreshCollectorClients(false);
      toast("收集后端配置已保存并刷新");
    };
    document.getElementById("btn-export-server-bundle").onclick = async () => {
      try {
        const res = await api().export_collector_server_bundle();
        toast(`已导出独立后端服务器包至: ${res.dir}`);
      } catch (e) {
        toast(`导出服务端包失败: ${e.message || e}`, "error");
      }
    };
    document.getElementById("btn-export-client-kit").onclick = async () => {
      try {
        const mode = document.getElementById("col-source-mode").value;
        const url = mode === "remote" ? document.getElementById("col-remote-url").value.trim() : "";
        const tok = (
          mode === "remote"
            ? document.getElementById("col-remote-token").value
            : document.getElementById("col-local-token").value
        ).trim();
        const res = await api().export_collector_client_kit(url, tok, "");
        toast(`已导出朋友矿机一键上报包至: ${res.dir}`);
      } catch (e) {
        toast(`导出客户端包失败: ${e.message || e}`, "error");
      }
    };
    document.getElementById("btn-col-refresh").onclick = () => refreshCollectorClients(false);
    document.getElementById("btn-col-delete-client").onclick = async () => {
      if (!state.colSelectedClientId) return;
      const ok = await confirmDialog(
        `确定要移除客户端节点 "${state.colSelectedClientId}" 的缓存与日志记录吗？若矿机仍在运行，下次心跳会重新注册。`,
        { title: "移除客户端节点", confirmText: "确认移除", danger: true }
      );
      if (!ok) return;
      try {
        await api().delete_collector_client(state.colSelectedClientId);
        state.colSelectedClientId = "";
        await refreshCollectorClients(true);
        toast("已移除节点记录");
      } catch (e) {
        toast(`移除失败: ${e.message || e}`, "error");
      }
    };
    document.getElementById("btn-col-copy-logs").onclick = () => {
      if (!colDetailLogs.length) return toast("该节点暂无日志", "warn");
      copyText(colDetailLogs.map((x) => x.line).join("\n"), "已复制远端客户端日志");
    };

    // Kernel Updater & .bat Configurator Controls
    document.getElementById("btn-kernel-browse-dir").onclick = async () => {
      const cur = (document.getElementById("kernel-dir-input")?.value || "").trim();
      const res = await api().browse_local_folder(cur);
      if (!res.cancelled && res.path) {
        document.getElementById("kernel-dir-input").value = res.path;
        await loadKernelView(res.path, true);
        toast("已绑定挖矿内核目录");
      }
    };
    document.getElementById("btn-kernel-inspect").onclick = async () => {
      const dir = (document.getElementById("kernel-dir-input")?.value || "").trim();
      if (!dir) return toast("请先选择或填写内核目录", "warn");
      await loadKernelView(dir, true);
      toast("内核目录检测完成");
    };
    document.getElementById("btn-kernel-sync-home-dir").onclick = async () => {
      const homeBat = (state.data?.settings?.local_bat_path || "").trim();
      if (!homeBat) return toast("主页尚未绑定 .bat 脚本", "warn");
      const sep = homeBat.lastIndexOf("\\") >= 0 ? homeBat.lastIndexOf("\\") : homeBat.lastIndexOf("/");
      if (sep <= 0) return toast("无法从主页脚本解析所在目录", "warn");
      const dir = homeBat.slice(0, sep);
      document.getElementById("kernel-dir-input").value = dir;
      await loadKernelView(dir, true);
      await loadKernelBatByPath(homeBat);
    };
    document.getElementById("btn-kernel-open-dir").onclick = () => {
      const dir = (document.getElementById("kernel-dir-input")?.value || "").trim();
      if (!dir) return toast("请先输入或选择内核目录", "warn");
      api().open_folder_path(dir);
    };
    document.getElementById("btn-kernel-check-updates").onclick = checkKernelUpdates;
    document.getElementById("kernel-release-select").onchange = renderSelectedReleaseDetail;
    document.getElementById("btn-kernel-open-release-url").onclick = () => {
      if (state.kernelLatestHtmlUrl) api().open_url(state.kernelLatestHtmlUrl);
    };
    document.getElementById("btn-kernel-update-exe-only").onclick = () => triggerKernelDeploy("exe_only");
    document.getElementById("btn-kernel-full-deploy").onclick = () => triggerKernelDeploy("full_deploy");

    document.getElementById("kernel-bat-select").onchange = (e) => {
      if (e.target.value) loadKernelBatByPath(e.target.value);
    };
    document.getElementById("btn-bat-browse").onclick = async () => {
      const res = await api().browse_local_file("bat");
      if (!res.cancelled && res.path) {
        const sep = res.path.lastIndexOf("\\") >= 0 ? res.path.lastIndexOf("\\") : res.path.lastIndexOf("/");
        if (sep > 0) {
          const d = res.path.slice(0, sep);
          document.getElementById("kernel-dir-input").value = d;
          await loadKernelView(d, true);
        }
        await loadKernelBatByPath(res.path);
      }
    };
    document.getElementById("btn-bat-fill-wallet").onclick = () => {
      const addr = state.data?.active_wallet?.address || "";
      if (!addr) return toast("当前没有激活钱包", "warn");
      document.getElementById("bat-wallet").value = addr;
      refreshBatPreview();
      toast("已填入当前软件激活钱包地址");
    };
    [
      "bat-executable",
      "bat-algorithm",
      "bat-pool",
      "bat-wallet",
      "bat-worker",
      "bat-password",
      "bat-log-file",
      "bat-api-port",
      "bat-extra-args",
      "bat-preamble",
      "bat-disable-cpu",
      "bat-api-enable",
      "bat-has-loop",
      "bat-has-pause",
    ].forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.addEventListener("input", () => refreshBatPreview());
      el.addEventListener("change", () => refreshBatPreview());
    });
    const rawEditor = document.getElementById("bat-raw-preview");
    if (rawEditor) {
      rawEditor.addEventListener("input", () => {
        const chk = document.getElementById("bat-manual-raw");
        if (chk && !chk.checked) chk.checked = true;
      });
    }
    document.getElementById("btn-bat-refresh-preview").onclick = () => refreshBatPreview(true);
    document.getElementById("btn-bat-save").onclick = () => saveKernelBatAction({ bindAsHomeBat: false, startNow: false });
    document.getElementById("btn-bat-save-bind").onclick = () => saveKernelBatAction({ bindAsHomeBat: true, startNow: false });
    document.getElementById("btn-bat-save-run").onclick = () => saveKernelBatAction({ bindAsHomeBat: true, startNow: true });

    // Notification Center Controls
    state.notifications = loadStoredNotifications();
    renderNotifBadge();
    document.getElementById("btn-notif-center").onclick = () => openNotifDrawer(false);
    document.getElementById("btn-notif-close").onclick = closeNotifDrawer;
    document.getElementById("notif-drawer-backdrop").onclick = (e) => {
      if (e.target.id === "notif-drawer-backdrop") closeNotifDrawer();
    };
    document.getElementById("btn-notif-mark-read").onclick = () => {
      state.notifications.forEach((n) => {
        n.read = true;
      });
      persistNotifications();
      renderNotifDrawerList();
    };
    document.getElementById("btn-notif-clear").onclick = async () => {
      if (!(state.notifications || []).length) return;
      const ok = await confirmDialog("确定清空全部历史通知记录吗？", {
        title: "清空通知中心",
        confirmText: "清空",
        danger: true,
      });
      if (!ok) return;
      state.notifications = [];
      persistNotifications();
      renderNotifDrawerList();
    };
    document.querySelectorAll("#notif-filter-pills .pill").forEach((btn) => {
      btn.onclick = () => {
        document.querySelectorAll("#notif-filter-pills .pill").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.notifFilter = btn.dataset.nfilter || "all";
        renderNotifDrawerList();
      };
    });
    document.getElementById("notif-search-input").oninput = (e) => {
      state.notifSearch = e.target.value || "";
      renderNotifDrawerList();
    };
    document.getElementById("notif-list").onclick = (e) => {
      const btn = e.target.closest("[data-nact]");
      if (!btn) return;
      const act = btn.dataset.nact;
      const nid = btn.dataset.nid;
      const item = (state.notifications || []).find((n) => n.id === nid);
      if (!item) return;

      if (act === "read") {
        item.read = true;
        persistNotifications();
        renderNotifDrawerList();
      } else if (act === "delete") {
        state.notifications = state.notifications.filter((n) => n.id !== nid);
        persistNotifications();
        renderNotifDrawerList();
      } else if (act === "copy") {
        item.read = true;
        persistNotifications();
        renderNotifDrawerList();
        copyText(`[${item.ts}] [${item.category}] ${item.msg}`, "已复制通知内容", { skipCenter: true });
      } else if (act === "jump") {
        item.read = true;
        persistNotifications();
        closeNotifDrawer();
        if (btn.dataset.view) switchView(btn.dataset.view);
      }
    };

    setLocalMinerMode("bat");

    // Offline Alerts, Remote Credentials & Synology Standby Failover
    const alertProvEl = document.getElementById("alert-provider");
    if (alertProvEl) alertProvEl.onchange = syncAlertProviderBoxes;

    const btnSaveAlert = document.getElementById("btn-save-alert-cfg");
    if (btnSaveAlert) {
      btnSaveAlert.onclick = async () => {
        try {
          const payload = collectAlertConfigPayload();
          const res = await api().save_alert_config(payload);
          applyStatePayload(res.state);
          if (payload.standby_sync_enabled && res.standby_sync && !res.standby_sync.synced && !res.standby_sync.skipped) {
            toast(`告警配置已保存；群晖同步提示：${res.standby_sync.error || "连接异常"}`, "warn");
          } else {
            toast("掉线告警与群晖备援配置已保存！");
          }
        } catch (e) {
          toast(`保存告警配置失败: ${e.message || e}`, "error");
        }
      };
    }

    const btnTestAlert = document.getElementById("btn-send-test-alert");
    if (btnTestAlert) {
      btnTestAlert.onclick = async () => {
        try {
          const payload = collectAlertConfigPayload();
          const res = await api().send_test_alert(payload);
          applyStatePayload(res.state);
          if (res.result && res.result.ok) {
            toast("测试通知已发送，请查看微信群组 / 公众号消息！");
          } else {
            toast(`发送测试失败: ${(res.result && res.result.error) || "未知错误"}`, "error");
          }
        } catch (e) {
          toast(`发送测试失败: ${e.message || e}`, "error");
        }
      };
    }

    const btnReuseCol = document.getElementById("btn-alert-reuse-collector");
    if (btnReuseCol) {
      btnReuseCol.onclick = () => {
        const s = state.data?.settings || {};
        const colUrl = document.getElementById("col-remote-url")?.value?.trim() || s.collector_server_url || "";
        const colTok = document.getElementById("col-remote-token")?.value?.trim() || s.collector_token || "";
        if (!colUrl || colUrl.includes("127.0.0.1")) {
          return toast("集群后端当前为本机地址，请先填写群晖 NAS 的实际局域网/公网 IP", "warn");
        }
        document.getElementById("alert-standby-url").value = colUrl;
        if (colTok && !document.getElementById("alert-standby-token").value.trim()) {
          document.getElementById("alert-standby-token").value = colTok;
        }
        document.getElementById("alert-standby-enabled").checked = true;
        toast("已填入群晖后端地址，请点「保存告警配置」同步到群晖");
      };
    }

    const btnSyncStandby = document.getElementById("btn-sync-standby-now");
    if (btnSyncStandby) {
      btnSyncStandby.onclick = async () => {
        try {
          const payload = collectAlertConfigPayload();
          await api().save_alert_config(payload);
          const res = await api().sync_standby_now();
          applyStatePayload(res.state);
          if (res.sync && res.sync.synced) {
            toast("已与群晖 Docker 完成心跳和告警状态同步！");
          } else {
            toast(`同步失败：${(res.sync && res.sync.error) || "请确认群晖容器已启动并升级"}`, "error");
          }
        } catch (e) {
          toast(`检查群晖失败: ${e.message || e}`, "error");
        }
      };
    }

    const btnTestStandby = document.getElementById("btn-test-standby-alert");
    if (btnTestStandby) {
      btnTestStandby.onclick = async () => {
        try {
          const payload = collectAlertConfigPayload();
          await api().save_alert_config(payload);
          const res = await api().send_standby_test_alert(payload);
          applyStatePayload(res.state);
          if (res.result && res.result.ok) {
            toast("群晖 Docker 已成功发出一封测试推送！");
          } else {
            toast(`群晖发测试失败: ${(res.result && res.result.error) || "发送失败"}`, "error");
          }
        } catch (e) {
          toast(`群晖发测试失败: ${e.message || e}`, "error");
        }
      };
    }

    const btnAlertExportDocker = document.getElementById("btn-alert-export-docker");
    if (btnAlertExportDocker) {
      btnAlertExportDocker.onclick = async () => {
        try {
          const res = await api().export_collector_server_bundle();
          toast(`已导出新版群晖 Docker 包（含 alert_engine.py）至：${res.dir}`, "ok", { duration: 10000 });
        } catch (e) {
          toast(`导出失败: ${e.message || e}`, "error");
        }
      };
    }

    const btnClearAlertLogs = document.getElementById("btn-clear-alert-logs");
    if (btnClearAlertLogs) {
      btnClearAlertLogs.onclick = async () => {
        const res = await api().clear_alert_logs();
        applyStatePayload(res.state);
        toast("已清空告警发送历史");
      };
    }
  }

  // Expose hook for backend push notifications
  window.__onBackendRefresh = () => {
    loadState();
  };

  window.addEventListener("pywebviewready", async () => {
    bindEvents();
    await loadState();
    // High-resolution 200ms check gives crisp 1s steps with zero timer jitter or skipped seconds
    setInterval(tickCountdown, 200);
    // Live 1.2s incremental tailing for bound local SRBMiner console & real hashrate report
    setInterval(() => {
      pollLocalMinerStatus(false);
    }, 1200);
    // Live 3s refresh for distributed collector cluster view
    setInterval(() => {
      if (state.currentView === "collector" && document.getElementById("col-auto-refresh")?.checked) {
        refreshCollectorClients(true);
      }
    }, 3000);
    // Gentle 30s safety resync (poller pushes updates immediately after each cycle)
    setInterval(() => {
      if (state.data && state.data.active_wallet) {
        loadState(state.data.active_wallet.id);
      }
    }, 30000);
  });
})();
