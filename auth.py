from __future__ import annotations

from typing import Callable

from playwright.sync_api import BrowserContext, Playwright, TimeoutError as PWTimeout

import config


def _on_login_page(url: str) -> bool:
    u = (url or "").lower()
    if "esia" in u or "gosuslugi" in u:
        return True
    if "login-page" in u or "/auth/" in u:
        return True
    return False


def _is_blank(url: str) -> bool:
    """about:blank, data:, пустая строка — страница ещё не загрузилась."""
    u = (url or "").lower()
    return not u or u in ("about:blank", "about:newtab") or u.startswith("data:")


def _is_bars(url: str) -> bool:
    return "es.ciur.ru" in (url or "").lower()


def _wait_for_navigation_settle(page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except PWTimeout:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except PWTimeout:
        pass


def _safe_goto(page, url: str, log: Callable[[str], None], attempts: int = 2) -> None:
    """goto с retry и явным wait_until=domcontentloaded."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
            return
        except Exception as exc:
            last_exc = exc
            log(f"  goto({url}) попытка {i+1}/{attempts}: {exc}")
    if last_exc is not None:
        raise last_exc


def ensure_session(
    playwright: Playwright,
    *,
    confirm_login: Callable[[], None],
    log: Callable[[str], None] = print,
) -> BrowserContext:
    """Открывает БАРС в Chromium с постоянным профилем.

    Cookies/localStorage хранятся в `config.PROFILE_DIR` между запусками,
    поэтому Госуслуги логинятся вручную только когда сессия протухла.
    """
    config.PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    log(f"Запускаю Chromium из профиля {config.PROFILE_DIR}…")
    ctx = playwright.chromium.launch_persistent_context(
        user_data_dir=str(config.PROFILE_DIR),
        headless=False,
        viewport=None,  # натуральный размер окна
        accept_downloads=True,
    )
    ctx.set_default_timeout(config.ACTION_TIMEOUT_MS)
    ctx.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)

    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.bring_to_front()
    log(f"Открываю {config.BARS_URL} (стартовый URL: {page.url!r})…")
    try:
        _safe_goto(page, config.BARS_URL, log)
        _wait_for_navigation_settle(page)
    except Exception as exc:
        log(f"Не удалось открыть БАРС: {exc}. Подождите и нажмите «Я вошёл (продолжить)».")

    log(f"URL после goto: {page.url!r}")

    # Сессия активна, только если URL похож на БАРС и НЕ редиректит на login.
    if _is_bars(page.url) and not _on_login_page(page.url):
        log("Сессия из профиля действует, вход не требуется.")
        return ctx

    # Иначе — нужен ручной вход. Если страница пустая (about:blank) — это либо
    # сетевая ошибка, либо первый запуск без профиля. Просим пользователя войти.
    if _is_blank(page.url):
        log("Страница не загрузилась (about:blank). Возможно, нет сети или БАРС недоступен.")
    log("Откройте окно браузера и войдите в БАРС через Госуслуги.")
    log("Когда увидите главную страницу журнала — нажмите «Я вошёл (продолжить)».")
    confirm_login()

    # Ищем активную страницу: после ESIA-редиректов могут появиться доп. вкладки.
    active_page = page
    for p in ctx.pages:
        try:
            url_p = p.url
        except Exception:
            continue
        if _is_bars(url_p) and not _on_login_page(url_p):
            active_page = p
            break

    try:
        active_page.bring_to_front()
        # Если активная страница всё ещё на login или blank — снова гоним на БАРС.
        if _on_login_page(active_page.url) or _is_blank(active_page.url):
            log("Перевожу активную страницу на главную БАРС…")
            _safe_goto(active_page, config.BARS_URL, log)
            _wait_for_navigation_settle(active_page)
    except Exception as exc:
        log(f"goto({config.BARS_URL}) после входа: {exc}")

    log(f"URL после входа: {active_page.url!r}")
    if _on_login_page(active_page.url) or _is_blank(active_page.url):
        log(
            "ВНИМАНИЕ: URL всё ещё похож на страницу логина или пустой. "
            "Профиль сохранён, но автозаход может не сработать в следующий раз — "
            "если понадобится, удалите папку chrome_profile/ и войдите заново."
        )
    else:
        log("Профиль сохранён — следующий запуск пройдёт без ручного входа.")

    return ctx
