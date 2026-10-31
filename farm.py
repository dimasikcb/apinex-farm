#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
APInex XP farmer — ежедневный чек-ин и сбор XP на https://apinex.bond/airdrop?tab=quests

Как работает:
  * один раз логинишься руками (режим login) -> профиль сохраняется в profile/
  * дальше скрипт в фоне открывает страницу тем же профилем, ждёт, пока Cloudflare
    Turnstile сам выдаст токен в модалке чек-ина, и отправляет те же запросы, что и сайт:
        POST /api/user/xp/checkin        {turnstileToken}
        POST /api/user/xp/quests/claim   {questKey}
  * капча не решается и не обходится: берётся токен, который браузер получил сам.

Режимы:
  login    — окно, вход через Google (один раз)
  run      — забрать всё доступное без окна
  watch    — резидентный режим: раз в 30 мин, один сбор в день
  inspect  — диагностика: состояние, кнопки, API-вызовы, скриншот
"""

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import msvcrt
except ImportError:  # не Windows — работаем без файловых блокировок
    msvcrt = None

BASE = Path(__file__).resolve().parent
PROFILE = BASE / "profile"
LOGS = BASE / "logs"
SHOTS = BASE / "shots"
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
SESSION_PATH = BASE / "session.json"
LOCK_PATH = BASE / ".running"
WATCH_LOCK = BASE / ".watch.lock"

DEFAULT_CONFIG = {
    "url": "https://apinex.bond/airdrop?tab=quests",
    "headless": True,
    "locale": "en-US",
    "timezone": "Europe/Moscow",
    "page_timeout_ms": 45000,
    "navigation_wait_ms": 4000,
    "turnstile_wait_sec": 20,
    "human_wait_sec": 180,
    "login_wait_sec": 3600,
}

STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


# ------------------------------------------------------------------ утилиты

def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / f"{datetime.now():%Y-%m-%d}.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            log(f"config.json битый ({e}), беру дефолт")
    return cfg


def read_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(**kw):
    state = read_state()
    state.update(kw)
    try:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"не смог записать state.json: {e}")


def utc_today():
    return f"{datetime.now(timezone.utc):%Y-%m-%d}"


def next_utc_midnight():
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def sleep_until(target):
    """Спит до момента target (UTC), проверяясь каждые 5 минут."""
    while True:
        left = (target - datetime.now(timezone.utc)).total_seconds()
        if left <= 2:
            return
        time.sleep(min(300, left))


def pid_alive(pid):
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def idle_seconds():
    """Сколько секунд назад юзер трогал мышь/клавиатуру."""
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        li = LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            return 0.0
        return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0
    except Exception:
        return 0.0


_held_locks = {}


def take_lock(path):
    """Эксклюзивная блокировка файла ОС: снимается сама, когда процесс умирает.

    PID-файл для этого не годится: Windows переиспользует номера, и новый процесс
    решает, что старый ещё жив.
    """
    if msvcrt is None:
        return True
    f = open(path, "a+b")
    f.seek(0)
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return False
    _held_locks[str(path)] = f
    return True


# ------------------------------------------------------------------ браузер

def launch(playwright, cfg, headless):
    PROFILE.mkdir(parents=True, exist_ok=True)
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
    ]
    kwargs = dict(
        user_data_dir=str(PROFILE),
        headless=headless,
        args=args,
        locale=cfg["locale"],
        timezone_id=cfg["timezone"],
        ignore_https_errors=True,
    )
    if headless:
        kwargs["viewport"] = {"width": 1440, "height": 900}
    else:
        # обычное окно в видимой области: его видно, если понадобится клик по капче
        kwargs["viewport"] = None
        args += ["--start-maximized", "--window-position=80,60", "--window-size=1400,950"]
    ctx = playwright.chromium.launch_persistent_context(**kwargs)
    ctx.add_init_script(STEALTH)
    ctx.set_default_timeout(20000)
    return ctx


def show_window(page):
    """Возвращает окно на экран: Chrome помнит старую позицию (у нас была за краем экрана)."""
    try:
        cdp = page.context.new_cdp_session(page)
        wid = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        cdp.send("Browser.setWindowBounds",
                 {"windowId": wid, "bounds": {"left": 60, "top": 40, "width": 1320, "height": 840}})
        try:
            page.bring_to_front()
        except Exception:
            pass
        return True
    except Exception as e:
        log(f"окно не удалось вывести на экран: {e}")
        return False


def save_session(ctx):
    """Держим cookie отдельно: в профиле они иногда теряются (сессионные не пишутся на диск)."""
    try:
        cookies = [c for c in ctx.cookies() if "apinex.bond" in (c.get("domain") or "")]
        if cookies:
            SESSION_PATH.write_text(json.dumps(cookies, ensure_ascii=False, indent=1), encoding="utf-8")
            return len(cookies)
    except Exception as e:
        log(f"cookies не сохранились: {e}")
    return 0


def restore_session(ctx):
    if not SESSION_PATH.exists():
        return 0
    try:
        cookies = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        ctx.add_cookies(cookies)
        return len(cookies)
    except Exception as e:
        log(f"cookies не восстановились: {e}")
        return 0


def goto(page, url, cfg):
    try:
        page.goto(url, wait_until="commit", timeout=cfg["page_timeout_ms"])
    except Exception as e:
        log(f"goto: {type(e).__name__} (продолжаю)")
    try:
        page.wait_for_selector("body", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(cfg["navigation_wait_ms"])


def api(page, path, method="GET", payload=None):
    """Запрос из контекста страницы — с её cookies и заголовками."""
    js = """
    async ([path, method, payload]) => {
        try {
            const opt = {method, credentials: 'include', headers: {}};
            if (payload !== null) {
                opt.headers['Content-Type'] = 'application/json';
                opt.body = JSON.stringify(payload);
            }
            const r = await fetch(path, opt);
            const txt = await r.text();
            let body = null;
            try { body = JSON.parse(txt); } catch (e) { body = txt.slice(0, 400); }
            return {status: r.status, body};
        } catch (e) {
            return {status: -1, body: String(e)};
        }
    }
    """
    try:
        return page.evaluate(js, [path, method, payload])
    except Exception as e:
        return {"status": -1, "body": str(e)}


def get_state(page):
    """Собирает XP и квесты. Возвращает dict или None, если не авторизован."""
    me = api(page, "/api/user/me")
    if me.get("status") != 200:
        return None
    xp = api(page, "/api/user/xp/me")
    quests = api(page, "/api/user/xp/quests")
    return {
        "email": ((me.get("body") or {}).get("user") or {}).get("email"),
        "total_xp": (xp.get("body") or {}).get("totalXp"),
        "tier": (xp.get("body") or {}).get("tier"),
        "checkin": (quests.get("body") or {}).get("checkin") or {},
        "daily": (quests.get("body") or {}).get("daily") or {},
    }


# ------------------------------------------------------------------ чек-ин

CHECKIN_BTN_JS = """
() => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const bad = /(come back|tomorrow|claimed|later|close|tomorrow)/;
  const good = /(check.?in|claim|daily|streak)/;
  const nodes = Array.from(document.querySelectorAll('button, [role="button"], a[href], [class*="card"]'));
  const out = [];
  let i = 0;
  for (const el of nodes) {
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 20) continue;
    const t = norm(el.innerText || el.getAttribute('aria-label') || '');
    if (!t || t.length > 120) continue;
    if (!good.test(t) || bad.test(t)) continue;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    el.setAttribute('data-ci', String(i));
    out.push({id: i, text: t.slice(0, 70), tag: el.tagName.toLowerCase()});
    i++;
  }
  return out;
}
"""


def open_checkin_modal(page):
    """Жмёт карточку чек-ина, чтобы отрисовалась модалка с Turnstile."""
    try:
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
        cands = page.evaluate(CHECKIN_BTN_JS)
    except Exception as e:
        log(f"  поиск кнопки чек-ина: {type(e).__name__}")
        return False
    if not cands:
        log("  кнопка чек-ина не найдена (вероятно, уже забрано)")
        return False
    for c in cands:
        try:
            loc = page.locator(f'[data-ci="{c["id"]}"]').first
            loc.scroll_into_view_if_needed(timeout=4000)
            page.wait_for_timeout(300)
            loc.click(timeout=6000)
            page.wait_for_timeout(2500)
            if page.locator('input[name="cf-turnstile-response"]').count() or \
               page.locator('[class*="modal"], [role="dialog"]').count():
                log(f"  модалка открыта: «{c['text']}»")
                return True
        except Exception:
            continue
    log("  модалка не открылась")
    return False


def wait_turnstile_token(page, timeout_sec):
    """Ждёт, пока Cloudflare сам выдаст токен. Ничего не решаем — просто читаем."""
    deadline = time.time() + timeout_sec
    last = None
    while time.time() < deadline:
        try:
            tok = page.evaluate(
                """() => {
                    const el = document.querySelector('input[name="cf-turnstile-response"]');
                    return el ? (el.value || '') : '';
                }"""
            )
        except Exception:
            tok = ""
        if tok and len(tok) > 20:
            return tok
        last = tok
        page.wait_for_timeout(1500)
    return None


def has_interactive_widget(page):
    """Cloudflare показывает чекбокс «Verify you are human» — значит, нужен клик."""
    try:
        return page.evaluate(
            """() => {
                const frames = Array.from(document.querySelectorAll('iframe'))
                    .map(f => f.src || '');
                if (frames.some(s => s.includes('challenges.cloudflare.com'))) return true;
                return /verify you are human/i.test(document.body.innerText || '');
            }"""
        )
    except Exception:
        return False


def do_checkin(page, cfg, state, human=False, probe_only=False):
    ci = state.get("checkin") or {}
    today = utc_today()
    if not ci.get("canClaim"):
        if ci.get("dayKey") == today and ci.get("checkedInToday"):
            log(f"чек-ин уже забран (streak {ci.get('currentStreak')}, +{ci.get('claimedXp')} XP)")
            return "already"
        log(f"день на сервере ещё не сменился (dayKey {ci.get('dayKey')}, UTC {today}) — подожду")
        return "waiting_day"
    if probe_only:
        log(f"чек-ин доступен: +{ci.get('rewardXp')} XP — нужен клик по капче")
        return "need_human"
    log(f"чек-ин доступен: +{ci.get('rewardXp')} XP (streak {ci.get('nextStreak')})")
    if not open_checkin_modal(page):
        return "failed"

    if human:
        try:
            page.bring_to_front()
        except Exception:
            pass
        log("  >>> КЛИКНИ «Verify you are human» в открытом окне <<<")
        log(f"  жду до {cfg['human_wait_sec']} с")

    tok = wait_turnstile_token(page, cfg["human_wait_sec"] if human else cfg["turnstile_wait_sec"])
    if not tok:
        if human:
            log("  клика не дождался — закрою окно, попробую позже")
            return "need_human"
        log("  токена Turnstile нет (headless капчу не проходит) — нужен клик человека")
        return "need_human"
    log(f"  токен Turnstile получен ({len(tok)} симв.)")
    res = api(page, "/api/user/xp/checkin", "POST", {"turnstileToken": tok})
    log(f"  POST checkin -> {res.get('status')} {str(res.get('body'))[:180]}")
    return "claimed" if res.get("status") == 200 else "failed"


def do_quests(page, state):
    daily = state.get("daily") or {}
    quests = daily.get("quests") or []
    claimed = 0
    for q in quests:
        if not q.get("claimable"):
            continue
        log(f"квест «{q.get('title')}»: забираю {q.get('xpPreview')} XP")
        res = api(page, "/api/user/xp/quests/claim", "POST", {"questKey": q.get("key")})
        log(f"  POST claim {q.get('key')} -> {res.get('status')} {str(res.get('body'))[:150]}")
        if res.get("status") == 200:
            claimed += 1
    if not claimed:
        log(f"квестов к получению нет (выполнено {daily.get('completed')}/{daily.get('total')})")
    return claimed


# ------------------------------------------------------------------ прогон

def run_once(cfg, headless, human=False, probe_only=False):
    """Один прогон. Возвращает (code, result_dict)."""
    from playwright.sync_api import sync_playwright
    started = datetime.now()
    result = {"clicks": 0, "checkin": False, "quests": 0}
    with sync_playwright() as p:
        ctx = launch(p, cfg, headless=headless)
        restore_session(ctx)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if not headless:
            show_window(page)
        try:
            goto(page, cfg["url"], cfg)
            state = get_state(page)
            if not state:
                page.screenshot(path=str(SHOTS / "logged-out.png"))
                log("НЕ ЗАЛОГИНЕН — запусти один раз: farm.py login")
                return 2, {"status": "logged_out"}
            result["xp_before"] = state["total_xp"]
            result["email"] = state.get("email")
            result["tier"] = state.get("tier")
            result["streak"] = (state.get("checkin") or {}).get("currentStreak")
            save_session(ctx)
            log(f"аккаунт {state['email']} | XP {state['total_xp']} ({state['tier']})")

            ci_res = do_checkin(page, cfg, state, human=human, probe_only=probe_only)
            result["checkin"] = ci_res == "claimed"
            result["checkin_state"] = ci_res
            result["quests"] = do_quests(page, state)

            after = get_state(page) or {}
            result["xp_after"] = after.get("total_xp")
            try:
                page.screenshot(path=str(SHOTS / f"run-{started:%Y%m%d-%H%M}.png"), full_page=True)
            except Exception:
                pass
            log(f"XP после: {result['xp_after']} (было {result['xp_before']})")
            result["status"] = "ok" if ci_res in ("claimed", "already") else ci_res
            result["day_key"] = (state.get("checkin") or {}).get("dayKey")
            return 0, result
        except Exception as e:
            log(f"ОШИБКА: {type(e).__name__}: {e}")
            log(traceback.format_exc(limit=3))
            try:
                page.screenshot(path=str(SHOTS / "error.png"), full_page=True)
            except Exception:
                pass
            return 1, {"status": f"error: {e}"}
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def persist(started, res):
    """Общая запись результата: state.json + история + панель."""
    save_state(last_run=f"{started:%Y-%m-%d %H:%M}",
               duration_sec=round((datetime.now() - started).total_seconds(), 1), **res)
    if res.get("status") == "ok" and res.get("day_key"):
        save_state(claimed_utc_day=res["day_key"])
    hist = read_state().get("history") or []
    hist.insert(0, {
        "t": f"{started:%Y-%m-%d %H:%M}",
        "status": res.get("status"),
        "checkin": res.get("checkin_state"),
        "xp": res.get("xp_after"),
        "quests": res.get("quests"),
    })
    save_state(history=hist[:30])
    try:
        build_panel()
    except Exception as e:
        log(f"панель не собралась: {e}")


def open_in_browser(url):
    """Открывает страницу в обычном браузере пользователя — там капча проходит."""
    try:
        os.startfile(url)
        return True
    except Exception as e:
        log(f"не смог открыть браузер: {e}")
        return False


def probe(cfg):
    """Быстрая проверка состояния без окна и без модалки."""
    started = datetime.now()
    if not take_lock(BASE / ".run.lock"):
        log("идёт другой прогон — пропускаю проверку")
        return 3, {}
    try:
        code, res = run_once(cfg, headless=True, probe_only=True)
        persist(started, res)
        return code, res
    finally:
        lock = _held_locks.pop(str(BASE / ".run.lock"), None)
        if lock:
            try:
                lock.close()
            except Exception:
                pass


def do_run(cfg, headless=None, force=False):
    headless = cfg["headless"] if headless is None else headless
    SHOTS.mkdir(parents=True, exist_ok=True)
    started = datetime.now()

    if not take_lock(BASE / ".run.lock"):
        log("уже идёт другой прогон — выхожу")
        return 3

    log(f"— старт ({'headless' if headless else 'окно'}) —")
    try:
        code, res = run_once(cfg, headless, human=not headless, probe_only=headless)
        if code == 0 and res.get("checkin_state") == "need_human":
            log("открываю окно — нужен клик по капче")
            code, res = run_once(cfg, headless=False, human=True)
        persist(started, res)
        log("— готово —" if code == 0 else f"— выход с кодом {code} —")
        return code
    finally:
        lock = _held_locks.pop(str(BASE / ".run.lock"), None)
        if lock:
            try:
                lock.close()
            except Exception:
                pass


# ------------------------------------------------------------------ панель

PANEL_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0d0f14;color:#e6e9ef;font:14px/1.5 -apple-system,'Segoe UI',Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:20px;margin:0 0 4px;letter-spacing:-.2px}
.sub{color:#7d8698;font-size:12px;margin-bottom:22px}
.pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
.ok{background:#14351f;color:#4ade80}
.bad{background:#3a1a1d;color:#f87171}
.warn{background:#3a2f12;color:#fbbf24}
.idle{background:#1c2230;color:#94a3b8}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:18px 0}
.card{background:#151922;border:1px solid #232a36;border-radius:12px;padding:14px 16px}
.card .k{color:#7d8698;font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.card .v{font-size:22px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
.card .v small{font-size:12px;color:#7d8698;font-weight:400}
.hint{border-left:3px solid #7c5cff;background:#171427;padding:12px 14px;border-radius:8px;margin:16px 0}
.hint b{color:#c4b5fd}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.7px;color:#7d8698;margin:26px 0 10px;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #1d2430}
th{color:#7d8698;font-weight:500;font-size:11px;text-transform:uppercase}
pre{background:#0a0c11;border:1px solid #1d2430;border-radius:10px;padding:14px;overflow:auto;max-height:380px;
    font:12px/1.55 'IBM Plex Mono',Consolas,monospace;color:#b9c2d0;margin:0}
.lg-err{color:#f87171}
.lg-ok{color:#4ade80}
.lg-warn{color:#fbbf24}
.shots{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.shots a{display:block;border:1px solid #232a36;border-radius:10px;overflow:hidden;background:#151922}
.shots img{width:100%;display:block;aspect-ratio:16/10;object-fit:cover;object-position:top}
.shots span{display:block;padding:6px 9px;font-size:11px;color:#7d8698}
code{background:#1c2230;padding:1px 6px;border-radius:5px;font-size:12px}
"""


def build_panel():
    """Собирает panel.html — автономный отчёт: XP, статус, лог, скриншоты."""
    st = read_state()
    today = f"{datetime.now():%Y-%m-%d}"
    logfile = LOGS / f"{today}.log"
    lines = []
    if logfile.exists():
        try:
            lines = logfile.read_text(encoding="utf-8", errors="replace").splitlines()[-140:]
        except Exception:
            lines = []

    def cls(ln):
        low = ln.lower()
        if "ошибка" in low or "failed" in low or "не вышло" in low:
            return "lg-err"
        if "claimed" in low or "готово" in low or "забран" in low:
            return "lg-ok"
        if "клик" in low or "жду" in low or "капч" in low:
            return "lg-warn"
        return ""

    log_html = "\n".join(f'<span class="{cls(l)}">{html_escape(l)}</span>' for l in lines) or "лог пуст"

    status = st.get("status") or "—"
    badge = {"ok": ("ok", "забрано"), "need_human": ("warn", "нужен клик по капче"),
             "waiting_day": ("idle", "ждёт смены UTC-дня"), "logged_out": ("bad", "нет входа")}
    bcls, btext = badge.get(status, ("bad" if str(status).startswith("error") else "idle", status))

    hints = {
        "ok": "XP за сегодня забрано. Следующий заход — после 00:00 UTC (03:00 МСК).",
        "need_human": "Чек-ин доступен. Скрипт открывает страницу в твоём браузере — "
                      "забери вручную: карточка дня → галочка → Claim.",
        "waiting_day": "Сервер ещё не открыл новый UTC-день. Скрипт сам повторит.",
        "logged_out": "Сессия истекла. Запусти <code>login.bat</code> и войди через Google.",
    }
    hint = hints.get(status, "Смотри лог ниже.")

    shots = sorted(SHOTS.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[:6]
    shots_html = "\n".join(
        f'<a href="shots/{html_escape(p.name)}" target="_blank">'
        f'<img src="shots/{html_escape(p.name)}" loading="lazy">'
        f'<span>{html_escape(p.name)}</span></a>' for p in shots) or "<div class='sub'>пока нет</div>"

    hist = st.get("history") or []
    if not hist and st.get("last_run"):
        hist = [{"t": st.get("last_run"), "status": st.get("status"),
                 "checkin": st.get("checkin_state"), "xp": st.get("xp_after"),
                 "quests": st.get("quests")}]
    hist_html = "\n".join(
        f"<tr><td>{html_escape(str(h.get('t')))}</td><td>{html_escape(str(h.get('status')))}</td>"
        f"<td>{html_escape(str(h.get('checkin')))}</td><td>{h.get('xp') if h.get('xp') is not None else '—'}</td>"
        f"<td>{h.get('quests') if h.get('quests') is not None else '—'}</td></tr>"
        for h in hist[:15]) or "<tr><td colspan=5>пусто</td></tr>"

    xp = st.get("xp_after")
    xp_cell = f"{xp}" if xp is not None else "—"
    delta = None
    if st.get("xp_before") is not None and st.get("xp_after") is not None:
        delta = st["xp_after"] - st["xp_before"]
    delta_txt = f"<small>{'+' if (delta or 0) > 0 else ''}{delta}</small>" if delta else ""

    html = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>APInex XP — панель</title><style>{PANEL_CSS}</style></head>
<body><div class="wrap">
<h1>APInex XP <span class="pill {bcls}">{html_escape(btext)}</span></h1>
<div class="sub">{html_escape(str(st.get('email') or 'аккаунт неизвестен'))} · собрано {datetime.now():%d.%m %H:%M:%S}</div>

<div class="grid">
  <div class="card"><div class="k">XP</div><div class="v">{xp_cell} {delta_txt}</div></div>
  <div class="card"><div class="k">Серия</div><div class="v">{st.get('streak') or '—'} <small>дн.</small></div></div>
  <div class="card"><div class="k">Тир</div><div class="v">{html_escape(str(st.get('tier') or '—'))}</div></div>
  <div class="card"><div class="k">Последний прогон</div><div class="v" style="font-size:15px">{html_escape(str(st.get('last_run') or '—'))}</div></div>
  <div class="card"><div class="k">До сброса (UTC)</div><div class="v" id="cd" style="font-size:17px">—</div></div>
</div>

<div class="hint">{hint}</div>

<h2>Прогоны</h2>
<table><thead><tr><th>Когда</th><th>Статус</th><th>Чек-ин</th><th>XP</th><th>Квесты</th></tr></thead>
<tbody>{hist_html}</tbody></table>

<h2>Лог за сегодня</h2>
<pre>{log_html}</pre>

<h2>Скриншоты</h2>
<div class="shots">{shots_html}</div>

<script>
const target = new Date();
target.setUTCHours(24,0,0,0);
setInterval(()=>{{
  const d = Math.max(0, target - new Date());
  const h = Math.floor(d/3600000), m = Math.floor(d%3600000/60000), s = Math.floor(d%60000/1000);
  document.getElementById('cd').textContent = h+'ч '+String(m).padStart(2,'0')+'м '+String(s).padStart(2,'0')+'с';
}}, 1000);
</script>
</div></body></html>"""
    out = BASE / "panel.html"
    out.write_text(html, encoding="utf-8")
    return out


def html_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# ------------------------------------------------------------------ вход / диагностика

def do_login(cfg):
    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = launch(p, cfg, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        show_window(page)
        goto(page, "https://apinex.bond/login", cfg)
        print("\n=== Войди через Google в открытом окне ===", flush=True)
        print(f"=== Жду до {cfg['login_wait_sec'] // 60} минут, окно закроется само ===\n", flush=True)
        deadline = time.time() + cfg["login_wait_sec"]
        ok = False
        while time.time() < deadline:
            st = get_state(page)
            if st:
                ok = True
                break
            page.wait_for_timeout(3000)
        if ok:
            page.screenshot(path=str(SHOTS / "login-ok.png"))
            n = save_session(ctx)
            log(f"вход подтверждён: {st.get('email')}, XP {st.get('total_xp')}, cookies {n}")
            save_state(email=st.get("email"), tier=st.get("tier"))
            try:
                build_panel()
            except Exception:
                pass
            ctx.close()
            return 0
        try:
            page.screenshot(path=str(SHOTS / "login-failed.png"))
        except Exception:
            pass
        ctx.close()
    log("вход НЕ выполнен — повтори: farm.py login")
    return 1


def do_inspect(cfg):
    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = launch(p, cfg, headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        goto(page, cfg["url"], cfg)
        st = get_state(page)
        if not st:
            log("не залогинен")
            ctx.close()
            return 2
        info = page.evaluate(
            """() => ({
                url: location.href,
                buttons: Array.from(document.querySelectorAll('button')).map(b => ({
                    t: (b.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 60),
                    d: b.disabled, c: (b.className || '').toString().slice(0, 50)
                })).filter(x => x.t),
                turnstile: !!document.querySelector('input[name="cf-turnstile-response"]')
            })"""
        )
        page.screenshot(path=str(SHOTS / "inspect-full.png"), full_page=True)
        data = {"state": st, "page": info}
        (SHOTS / "inspect.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log(json.dumps(st, ensure_ascii=False)[:600])
        log(f"данные: {SHOTS / 'inspect.json'}")
        ctx.close()
    return 0


# ------------------------------------------------------------------ фоновый режим

def do_watch(cfg):
    if not take_lock(WATCH_LOCK):
        log("watch уже запущен — выхожу")
        return 0

    log(f"watch: старт, сейчас {datetime.now(timezone.utc):%H:%M} UTC")

    while True:
        st = read_state()
        today = utc_today()

        # день закрыт — браузер вообще не запускаем
        if st.get("claimed_utc_day") == today:
            target = next_utc_midnight()
            log(f"на {today} UTC забрано — сплю до {target:%Y-%m-%d %H:%M} UTC")
            sleep_until(target)
            log("сброс дня: проверяю")
            continue

        # нечего открывать, пока человека нет за компьютером
        if idle_seconds() > 300:
            time.sleep(120)
            continue

        code, res = probe(cfg)
        status = res.get("status")

        if status == "ok":
            continue

        if status == "need_human":
            log("чек-ин доступен — открываю страницу в твоём браузере")
            if open_in_browser(cfg["url"]):
                for _ in range(12):          # ждём до часа, проверяя каждые 5 минут
                    time.sleep(300)
                    _, r2 = probe(cfg)
                    if r2.get("status") == "ok":
                        log("забрано — ок")
                        break
                    if idle_seconds() > 900:
                        log("отошёл от компа — жду следующего раза")
                        break
            continue

        log(f"статус {status} — повтор через 15 минут")
        time.sleep(900)


def main():
    ap = argparse.ArgumentParser(description="APInex XP farmer")
    ap.add_argument("mode", nargs="?", default="run", choices=["login", "run", "inspect", "watch", "panel", "open"])
    ap.add_argument("--show", action="store_true", help="не скрывать окно браузера")
    ap.add_argument("--force", action="store_true", help="игнорировать блокировки")
    ap.add_argument("--url", help="переопределить адрес страницы")
    args = ap.parse_args()

    cfg = load_config()
    if args.url:
        cfg["url"] = args.url

    if args.mode == "login":
        return do_login(cfg)
    if args.mode == "inspect":
        return do_inspect(cfg)
    if args.mode == "watch":
        return do_watch(cfg)
    if args.mode == "panel":
        out = build_panel()
        log(f"панель: {out}")
        return 0
    if args.mode == "open":
        code, res = probe(cfg)
        st = res.get("status")
        if st == "ok":
            log("забирать нечего — уже забрано")
            return 0
        if st == "need_human":
            open_in_browser(cfg["url"])
            log("открыл страницу в браузере — забери вручную")
            return 0
        log(f"статус {st}")
        return 0
    return do_run(cfg, headless=False if args.show else None, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
