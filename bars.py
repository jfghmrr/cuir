from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout

import config
from parser import GroupSchedule, Lesson


def normalize_topic(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip().casefold()


_RUSSIAN_MONTHS_SHORT = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5,
    "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


def _excel_date_key(date_hint: str) -> str:
    """Нормализует Excel-дату вида '14 Мая' → 'DD.MM' ('14.05'). Возвращает '' если не парсится."""
    if not date_hint:
        return ""
    m = re.match(r"(\d+)\s+(\S+)", date_hint.strip().lower())
    if not m:
        return ""
    day = int(m.group(1))
    month_word = m.group(2)
    for stem, num in _RUSSIAN_MONTHS_SHORT.items():
        if month_word.startswith(stem):
            return f"{day:02d}.{num:02d}"
    return ""


def _bars_date_key(modal_title: str) -> str:
    """Из '14.05.2026 11:30 - Васильева...' извлекает 'DD.MM' ('14.05')."""
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.\d{4}", modal_title or "")
    if not m:
        return ""
    return f"{int(m.group(1)):02d}.{int(m.group(2)):02d}"


# «Стоп-слова» — мало информативны для определения схожести темы.
_STOPWORDS = frozenset({
    "и", "в", "на", "по", "к", "для", "из", "от", "до", "о", "об", "при",
    "теме", "тема", "том", "та", "те", "тот", "это", "эти", "так", "как",
    "что", "не", "ни", "за",
})


def _topic_words(s: str) -> set[str]:
    """Значащие слова темы (>2 букв, без стоп-слов и пунктуации)."""
    tokens = re.findall(r"[\wёЁ]+", (s or "").lower())
    return {t for t in tokens if len(t) > 2 and t not in _STOPWORDS}


def _has_common_words(a: str, b: str, min_words: int = 2) -> bool:
    """True, если у тем есть хотя бы `min_words` общих значащих слов."""
    return len(_topic_words(a) & _topic_words(b)) >= min_words


@dataclass
class FillResult:
    topic: str
    date_hint: str
    homework: str | None
    status: str  # "filled" | "skipped_existing" | "skipped_no_homework" | "not_found" | "error"
    note: str = ""
    marked_conducted: bool = False  # поставлена ли галочка «Урок проведен» в этом прогоне


class BarsClient:
    """Сценарий заполнения «Задание на следующий урок» в БАРС.

    Селекторы — best-guess по скриншотам. Запускайте сначала с dry_run=True
    и при необходимости поправьте локаторы (см. _LOCATORS_TODO).
    """

    def __init__(
        self,
        ctx: BrowserContext,
        *,
        dry_run: bool = True,
        log: Callable[[str], None] = print,
    ) -> None:
        self.ctx = ctx
        self.dry_run = dry_run
        self.log = log
        self.page: Page | None = None

    # ───────────── навигация ─────────────

    def open_journal(self, schedule: GroupSchedule) -> None:
        # Переиспользуем активную страницу из persistent context — иначе при new_page()
        # БАРС снова покажет приветственные модалки.
        page = None
        for p in self.ctx.pages:
            if "es.ciur.ru" in p.url.lower():
                page = p
                break
        if page is None:
            page = self.ctx.new_page()
            page.goto(config.BARS_URL)
        page.bring_to_front()
        page.set_default_timeout(config.ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass
        self.page = page

        self._dismiss_intro_modals()

        self.log("Открываю «Классный журнал»...")
        if not self._click_first_visible(
            page.get_by_text("Классный журнал"),
            description="иконка «Классный журнал»",
        ):
            raise RuntimeError("Не нашёл видимую иконку «Классный журнал» на главной")
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass

        self._dismiss_intro_modals()

        self._select_combobox("Период", schedule.period)
        self._select_combobox("Класс", schedule.klass)
        self._select_combobox("Предмет", schedule.subject)
        self._select_combobox("Группа", schedule.group)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass
        # Журнал догружается асинхронно — даём ему отрисовать таблицу.
        # Основное ожидание — в _lesson_column_headers (до 20 с).
        page.wait_for_timeout(3_000)

    def _dismiss_intro_modals(self) -> None:
        """Закрывает приветственные модалки БАРС («У вас есть непрочитанные сообщения» и т.п.)."""
        assert self.page is not None
        for _ in range(5):
            closed = False
            # Любая видимая кнопка «Закрыть» в модалке
            try:
                btn = self.page.get_by_role("button", name="Закрыть").first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    self.page.wait_for_timeout(300)
                    closed = True
            except Exception:
                pass
            if not closed:
                break

    def _click_first_visible(self, locator, description: str = "элемент") -> bool:
        """Кликает по первому видимому элементу из локатора.

        В БАРС часто несколько элементов с одинаковым текстом (плитка + пункт
        выпадающего меню), но видим обычно только один. Перебираем и берём
        первый is_visible.
        """
        try:
            elements = locator.all()
        except Exception:
            elements = []
        for el in elements:
            try:
                if not el.is_visible():
                    continue
                el.scroll_into_view_if_needed(timeout=2_000)
                el.click()
                return True
            except Exception as exc:
                self.log(f"  пропускаю невидимый/нерабочий вариант {description}: {exc}")
                continue
        return False

    def _select_combobox(self, label: str, option: str) -> None:
        """Открывает combobox по подписи и выбирает в нём пункт по тексту."""
        assert self.page is not None
        page = self.page
        self.log(f"  Выбираю {label!r} = {option!r}")

        field = self._find_combobox_field(label)
        if field is None:
            raise RuntimeError(f"Не нашёл поле с подписью {label!r}")

        # Открываем dropdown. Иногда клик по input не помогает — нужна стрелка
        # справа от поля. Пробуем оба варианта.
        try:
            field.click()
        except Exception:
            field.click(force=True)
        page.wait_for_timeout(500)
        # Дополнительно стрелка ArrowDown как универсальный способ открыть combobox.
        try:
            field.press("Alt+ArrowDown")
        except Exception:
            pass
        page.wait_for_timeout(300)

        # Диагностика: записать в лог все видимые тексты, похожие на наш option.
        if not self._click_option_in_dropdown(option):
            self._dump_dropdown_diagnostics(option)
            # Попробовать ввести текст для фильтрации, потом ещё раз поискать.
            try:
                field.fill("")
                field.type(option, delay=20)
                page.wait_for_timeout(1_200)  # ждём фильтрацию dropdown
            except Exception:
                pass
            if not self._click_option_in_dropdown(option):
                # Последний шанс — клавиатура: ArrowDown + Enter подберёт первое
                # отфильтрованное значение, если БАРС ставит подсветку при вводе.
                try:
                    field.press("ArrowDown")
                    page.wait_for_timeout(150)
                    field.press("Enter")
                    page.wait_for_timeout(300)
                    self.log("  пункт выбран через ArrowDown+Enter (как fallback)")
                    return
                except Exception:
                    pass
                raise RuntimeError(
                    f"Не нашёл пункт {option!r} в выпадающем списке {label!r}. "
                    "См. лог выше — должны быть выведены видимые кандидаты."
                )
        page.wait_for_timeout(300)

    def _click_option_in_dropdown(self, option: str) -> bool:
        """Пытается кликнуть по пункту с текстом option среди разных типов dropdown."""
        assert self.page is not None
        page = self.page
        candidates = [
            page.get_by_role("option", name=option, exact=True),
            page.locator("[role='option']", has_text=option),
            page.locator(
                ".x-boundlist-item, .x-combo-list-item, "
                "li[role='option'], li.x-list-item, [class*='dropdown'] [class*='item'], "
                "[class*='select'] [class*='option'], [class*='select'] [class*='item']"
            ).filter(has_text=option),
            # generic: любой видимый элемент с этим текстом, но НЕ inputs/labels
            page.locator(
                "li, div, span, a"
            ).filter(has_text=option),
        ]
        for cand in candidates:
            if self._click_first_visible(cand, description=f"пункт {option!r}"):
                return True
        return False

    def _dump_dropdown_diagnostics(self, option: str) -> None:
        """Логирует фрагмент DOM с текстами, похожими на option (для отладки селекторов)."""
        assert self.page is not None
        page = self.page
        # подстроку из 2-3 ключевых символов option ищем в видимых узлах
        hint = option.split()[-1] if option else option  # «Полугодие», «А», «англ»
        try:
            elements = page.locator(f":text('{hint}')").all()
        except Exception:
            elements = []
        self.log(f"  [diag] видимых элементов с подстрокой {hint!r}: {len(elements)}")
        shown = 0
        for el in elements:
            try:
                if not el.is_visible():
                    continue
                tag = el.evaluate("el => el.tagName") or "?"
                cls = el.evaluate("el => el.className") or ""
                text = (el.text_content() or "").strip().replace("\n", " ")[:80]
                self.log(f"  [diag] <{tag.lower()} class={cls!r}> '{text}'")
                shown += 1
                if shown >= 8:
                    break
            except Exception:
                continue

    def _find_combobox_field(self, label: str):
        """Находит видимое поле ввода/combobox по тексту-подписи."""
        assert self.page is not None
        page = self.page
        try:
            f = page.get_by_label(label, exact=True)
            if f.count() > 0 and f.first.is_visible():
                return f.first
        except Exception:
            pass
        labels = page.get_by_text(label, exact=True).all()
        for lab in labels:
            try:
                if not lab.is_visible():
                    continue
                near_input = lab.locator(
                    "xpath=following::*[self::input or @role='combobox'][1]"
                )
                if near_input.count() > 0 and near_input.first.is_visible():
                    return near_input.first
                wrapper_input = (
                    lab.locator(
                        "xpath=ancestor::*[descendant::input or descendant::*[@role='combobox']][1]"
                    )
                    .locator("input, [role='combobox']")
                    .first
                )
                if wrapper_input.count() > 0 and wrapper_input.is_visible():
                    return wrapper_input
            except Exception:
                continue
        return None

    # ───────────── основной алгоритм ─────────────

    def fill_homework(self, schedule: GroupSchedule) -> list[FillResult]:
        assert self.page is not None
        results: list[FillResult] = []

        # Сопоставление по теме: перебираем колонки-уроки в шапке журнала,
        # для каждой открываем карточку, читаем «Тему», ищем match среди уроков из Excel.
        # При дубликатах (одна тема в нескольких блоках Excel) предпочитаем запись
        # с непустым ДЗ, чтобы не перезатереть реальное ДЗ пустым шаблоном.
        targets: dict[str, Lesson] = {}
        for l in schedule.lessons:
            key = normalize_topic(l.topic)
            prev = targets.get(key)
            if prev is None:
                targets[key] = l
                continue
            prev_hw = (prev.homework or "").strip()
            new_hw = (l.homework or "").strip()
            if not prev_hw and new_hw:
                targets[key] = l

        already_done: set[str] = set()

        headers = self._lesson_column_headers()
        self.log(f"Найдено колонок-уроков в журнале: {len(headers)}")
        diag_shown = False

        for idx in range(len(headers)):
            # перечитываем headers каждый раз — DOM пересоздаётся после открытия модалки
            # _lesson_column_headers_once — без ожидания, журнал уже загружен
            current = self._lesson_column_headers_once()
            if idx >= len(current):
                break
            try:
                current[idx].click()
            except Exception as exc:
                self.log(f"  [колонка {idx}] не удалось открыть: {exc}")
                continue
            try:
                self.page.wait_for_load_state("networkidle", timeout=10_000)
            except PWTimeout:
                pass

            try:
                self._goto_lesson_tab()
                topic = self._read_lesson_topic()
            except Exception as exc:
                self.log(f"  [колонка {idx}] не удалось прочитать тему: {exc}")
                self._close_lesson_modal()
                continue

            self.log(f"  [колонка {idx}] тема в БАРС: {topic!r}")
            key = normalize_topic(topic)
            lesson = targets.get(key)

            if lesson is None:
                if not topic:
                    self.log("    тема пустая (нет привязки к КТП) → пропуск")
                else:
                    bars_dkey = self._read_lesson_modal_date()
                    date_note = f" (БАРС-дата: {bars_dkey})" if bars_dkey else ""
                    self.log(f"    тема не из Excel → not_found{date_note}")
                    # Конкретная диагностика: ищем похожие темы в Excel,
                    # чтобы пользователь увидел расхождение.
                    similar = [
                        ekey for ekey in targets
                        if _has_common_words(ekey, key, min_words=2)
                    ]
                    if similar:
                        self.log("    [diag] похожие темы в Excel:")
                        for ekey in similar[:5]:
                            self.log(f"    [diag]   {ekey!r}")
                    elif not diag_shown:
                        diag_shown = True
                        self.log("    [diag] полный список Excel-тем:")
                        for ekey in list(targets)[:30]:
                            self.log(f"    [diag]   {ekey!r}")
                self._close_lesson_modal()
                continue
            if key in already_done:
                self.log(f"    тема уже обработана в этом прогоне, пропускаю")
                self._close_lesson_modal()
                continue

            already_done.add(key)
            marked_conducted = False
            status: str = "skipped_no_homework"
            note: str = ""

            try:
                # 1. Галочка «Урок проведен» — ставим, если нужно (без сохранения).
                try:
                    marked_conducted = self._ensure_lesson_conducted()
                    if marked_conducted:
                        self.log(f"  ✓ {lesson.date_hint} | tick «Урок проведен»")
                    else:
                        self.log(f"    {lesson.date_hint} | галочка уже стояла")
                except Exception as exc:
                    self.log(f"  ! {lesson.date_hint} | не удалось поставить галочку: {exc}")

                # 2. ДЗ
                if lesson.homework is None or lesson.homework == "":
                    status = "skipped_no_homework"
                    self.log(f"    {lesson.date_hint} | ДЗ Excel пуст → пропуск")
                else:
                    status, note = self._add_next_lesson_homework(lesson.homework)
                    if status == "filled":
                        self.log(f"    {lesson.date_hint} | ДЗ добавлено")
                    elif status == "skipped_existing":
                        self.log(f"    {lesson.date_hint} | ДЗ уже есть → пропуск")

                # 3. Закрытие модалки — единая точка.
                if marked_conducted or status == "filled":
                    if self._save_and_close_lesson():
                        self.log(f"    {lesson.date_hint} | сохранено через «Сохранить и закрыть»")
                    else:
                        self.log("    fallback: «Сохранить и закрыть» не найдена — закрываю обычно")
                        self._close_lesson_modal()
                else:
                    self._close_lesson_modal()
                    self.log(f"    {lesson.date_hint} | закрыто без сохранения")

                results.append(
                    FillResult(
                        topic=topic,
                        date_hint=lesson.date_hint,
                        homework=lesson.homework,
                        status=status,
                        note=note,
                        marked_conducted=marked_conducted,
                    )
                )
            except Exception as exc:
                results.append(
                    FillResult(
                        topic=topic,
                        date_hint=lesson.date_hint,
                        homework=lesson.homework,
                        status="error",
                        note=str(exc),
                        marked_conducted=marked_conducted,
                    )
                )
                self.log(f"  ✗ {lesson.date_hint} | {topic[:50]}... — ошибка: {exc}")
                self._close_lesson_modal()

        # уроки, которые в Excel были, но в БАРС не нашли
        for key, lesson in targets.items():
            if key not in already_done:
                results.append(
                    FillResult(
                        topic=lesson.topic,
                        date_hint=lesson.date_hint,
                        homework=lesson.homework,
                        status="not_found",
                    )
                )
        return results

    # ───────────── элементы интерфейса ─────────────

    # _LOCATORS_TODO: уточнить селекторы по реальной разметке БАРС.
    # Пока — best-guess по скриншотам.

    # Текст заголовка колонки-урока: «14.01\n14:50», «29.01 12:20» и т.п.
    # Не анкорим (без ^ и $), т.к. заголовок может содержать лишний текст.
    _DATE_HEADER_RE = re.compile(r"\d{1,2}\.\d{1,2}[\s\S]{0,5}\d{1,2}:\d{2}")

    def _lesson_column_headers(self, max_wait_s: float = 20.0) -> list:
        """Возвращает видимые заголовки колонок-уроков, ожидая до max_wait_s секунд
        пока журнал загружается после выбора комбобоксов."""
        assert self.page is not None
        page = self.page

        deadline = time.monotonic() + max_wait_s
        logged_wait = False
        while True:
            result = self._lesson_column_headers_once()
            if result:
                return result
            if time.monotonic() >= deadline:
                break
            if not logged_wait:
                logged_wait = True
                self.log("  [ожидание] журнал загружается, жду до 20 секунд...")
            try:
                page.wait_for_timeout(800)
            except Exception:
                break

        # Ничего не нашли — диагностика.
        self._dump_journal_diagnostics()
        return []

    def _lesson_column_headers_once(self) -> list:
        """Один проход поиска заголовков (без ожидания). Возвращает [] если не найдено."""
        assert self.page is not None
        page = self.page

        strategies = [
            # 1. <th> или role=columnheader (стандартные таблицы)
            lambda: page.locator("th, [role='columnheader']").filter(
                has_text=self._DATE_HEADER_RE
            ),
            # 2. ExtJS / VueJS журналы: ячейки с классом *header* / *date* / *column*
            lambda: page.locator(
                "[class*='header'], [class*='Header'], [class*='date'], "
                "[class*='column'], .x-grid-cell-inner, .x-column-header-inner"
            ).filter(has_text=self._DATE_HEADER_RE),
            # 3. Любой div/span/td/th, содержащий паттерн дата+время
            lambda: page.locator("div, span, td, th").filter(
                has_text=self._DATE_HEADER_RE
            ),
        ]

        seen_keys: set[str] = set()
        for build in strategies:
            try:
                loc = build()
                visible = []
                for el in loc.all():
                    try:
                        if not el.is_visible():
                            continue
                        txt = (el.text_content() or "").strip()
                        if not self._DATE_HEADER_RE.search(txt):
                            continue
                        # Дедупликация по первым 20 символам текста
                        key = txt[:20]
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        visible.append(el)
                    except Exception:
                        continue
                if visible:
                    return visible
            except Exception:
                continue

        return []

    def _dump_journal_diagnostics(self) -> None:
        """Когда заголовки не найдены — показываем что вообще есть на странице."""
        assert self.page is not None
        page = self.page
        try:
            self.log(f"  [diag] URL страницы: {page.url!r}")
        except Exception:
            pass
        # Элементы <th>
        try:
            ths = page.locator("th").all()
            self.log(f"  [diag] элементов <th>: {len(ths)}")
            for th in ths[:8]:
                try:
                    text = (th.text_content() or "").strip().replace("\n", " ")[:60]
                    self.log(f"    <th> {text!r}")
                except Exception:
                    pass
        except Exception:
            pass
        # role=columnheader
        try:
            chs = page.get_by_role("columnheader").all()
            self.log(f"  [diag] role=columnheader: {len(chs)}")
            for ch in chs[:5]:
                try:
                    text = (ch.text_content() or "").strip().replace("\n", " ")[:60]
                    self.log(f"    columnheader: {text!r}")
                except Exception:
                    pass
        except Exception:
            pass
        # Любой видимый элемент с «DD.MM»
        try:
            elements = page.locator(":text-matches('\\d{1,2}\\.\\d{1,2}', '')").all()
            self.log(f"  [diag] элементов с «DD.MM»: {len(elements)}")
            shown = 0
            for el in elements:
                try:
                    if not el.is_visible():
                        continue
                    tag = (el.evaluate("el => el.tagName") or "?").lower()
                    cls = el.evaluate("el => el.className") or ""
                    text = (el.text_content() or "").strip().replace("\n", " ")[:80]
                    self.log(f"  [diag] <{tag} class={cls!r}> '{text}'")
                    shown += 1
                    if shown >= 10:
                        break
                except Exception:
                    continue
        except Exception:
            pass

    def _read_lesson_modal_date(self) -> str:
        """Возвращает 'DD.MM' из заголовка модалки '14.05.2026 11:30 - ...'.
        Пустую строку — если не нашли.
        """
        assert self.page is not None
        page = self.page
        try:
            # Заголовок модалки — h-элемент с датой; ищем первое вхождение DD.MM.YYYY HH:MM.
            loc = page.locator(
                ":text-matches('\\\\d{1,2}\\\\.\\\\d{1,2}\\\\.\\\\d{4}\\\\s+\\\\d{1,2}:\\\\d{2}', '')"
            )
            count = loc.count()
            for i in range(min(count, 5)):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    txt = (el.text_content() or "").strip()
                except Exception:
                    continue
                key = _bars_date_key(txt)
                if key:
                    return key
        except Exception:
            pass
        return ""

    def _goto_lesson_tab(self) -> None:
        """Переключиться на вкладку «Урок» в карточке урока (она же по умолчанию первая)."""
        assert self.page is not None
        try:
            tab = self.page.get_by_role("tab", name="Урок", exact=True)
            if tab.count() > 0:
                tab.first.click()
        except Exception:
            pass

    # Служебные подсказки в поле «Тема» — НЕ являются темой урока.
    # Только полные служебные фразы; не матчим частичные совпадения внутри
    # реальных тем (типа «Контроль по теме ... не заданы вопросы»).
    _TOPIC_NOISE_RE = re.compile(
        r"^(урок\s+не\s+привязан.*|не\s+выбран[ао]?|не\s+задан[ао]?|"
        r"выберите\s+тему|выбрать\s+тему|тема\s+не\s+задана?)$",
        re.IGNORECASE,
    )

    # JS, читающий значение из любого вида поля «Тема»: input, textarea,
    # contenteditable, Element-UI el-select (значение в .el-tag/.el-select__*).
    _TOPIC_EXTRACT_JS = """
    el => {
        const T = e => (e && (e.innerText || e.textContent) || '').trim();
        // 1. Сам элемент — input/textarea
        if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.value)
            return el.value;
        // 2. Сам элемент — contenteditable
        if (el.getAttribute && el.getAttribute('contenteditable') === 'true')
            return T(el);
        // 3. Внутри элемента — input.value
        const inputs = el.querySelectorAll('input, textarea');
        for (const i of inputs) if (i.value && i.value.trim()) return i.value.trim();
        // 4. Element-UI / Vue select: selected-tag / selection-item
        const sel = el.querySelector(
            '.el-select__tags span, .el-select__selected-item, ' +
            '.el-select__selection-item, .el-tag, .selection, [class*="selected"]'
        );
        if (sel) { const t = T(sel); if (t) return t; }
        // 5. contenteditable внутри
        const ce = el.querySelector('[contenteditable="true"]');
        if (ce) { const t = T(ce); if (t) return t; }
        // 6. Атрибуты как последний шанс
        return (el.value || (el.getAttribute && (el.getAttribute('placeholder') || el.getAttribute('title'))) || '');
    }
    """

    def _read_lesson_topic(self) -> str:
        """Читает тему из поля «Тема» на вкладке «Урок».

        БАРС грузит модалку асинхронно: подпись «Тема» уже в DOM, а value поля
        приходит с задержкой 100-1500ms (RPC-запрос на КТП). Делаем поллинг до
        3 секунд: каждые 150ms пробуем извлечь тему через JS-evaluate. Если
        реально пусто — возвращаем "" после таймаута.
        """
        assert self.page is not None
        page = self.page

        # Сначала дожидаемся появления подписи «Тема» в DOM — иначе поллим в пустоту.
        try:
            page.get_by_text("Тема", exact=True).first.wait_for(
                state="visible", timeout=3_000
            )
        except Exception:
            pass

        deadline = time.monotonic() + 5.0
        while True:
            topic = self._extract_topic_once()
            if topic:
                return topic
            if time.monotonic() >= deadline:
                return ""
            try:
                page.wait_for_timeout(150)
            except Exception:
                return ""

    def _extract_topic_once(self) -> str:
        """Один проход извлечения темы (без поллинга). Возвращает '' если пусто."""
        assert self.page is not None
        page = self.page

        def _accept(v) -> str:
            if not v:
                return ""
            v = str(v).strip()
            if not v:
                return ""
            if self._TOPIC_NOISE_RE.match(v):
                return ""
            return v

        # Стратегия 1: get_by_label — JS extract на каждом видимом кандидате
        try:
            for el in page.get_by_label("Тема", exact=True).all():
                try:
                    if not el.is_visible():
                        continue
                    raw = el.evaluate(self._TOPIC_EXTRACT_JS)
                except Exception:
                    continue
                accepted = _accept(raw)
                if accepted:
                    return accepted
        except Exception:
            pass

        # Стратегия 2: подпись «Тема» → ancestor-wrapper → JS extract
        try:
            for lab in page.get_by_text("Тема", exact=True).all():
                try:
                    if not lab.is_visible():
                        continue
                    wrapper = lab.locator(
                        "xpath=ancestor::*[descendant::input or "
                        "descendant::*[contains(@class,'el-select')] or "
                        "descendant::*[contains(@class,'el-tag')] or "
                        "descendant::*[@contenteditable='true']][1]"
                    )
                    if wrapper.count() == 0:
                        continue
                    raw = wrapper.first.evaluate(self._TOPIC_EXTRACT_JS)
                    accepted = _accept(raw)
                    if accepted:
                        return accepted
                except Exception:
                    continue
        except Exception:
            pass

        return ""

    def _ensure_lesson_conducted(self) -> bool:
        """Ставит галочку «Урок проведен», если она не стоит.

        Возвращает True, если галочка была поставлена в этом вызове.
        Сохранение модалки — единая точка через _save_and_close_lesson().
        """
        assert self.page is not None
        page = self.page

        checkbox = page.get_by_label("Урок проведен", exact=True)
        if checkbox.count() == 0:
            try:
                label = page.get_by_text("Урок проведен", exact=True).first
                checkbox = label.locator(
                    "xpath=preceding::*[@role='checkbox' or self::input[@type='checkbox']][1]"
                )
            except Exception:
                return False
        if checkbox.count() == 0:
            return False

        try:
            already = checkbox.first.is_checked()
        except Exception:
            # ExtJS-кастомные чекбоксы могут не отвечать на is_checked.
            aria = checkbox.first.get_attribute("aria-checked")
            already = (aria or "").lower() == "true"

        if already:
            return False

        if self.dry_run:
            self.log("    [DRY] would tick «Урок проведен»")
            return True

        try:
            checkbox.first.check()
        except Exception:
            # Кастомные чекбоксы могут не поддаваться .check() — пробуем JS click.
            try:
                checkbox.first.evaluate("el => el.click()")
            except Exception:
                return False

        # БАРС асинхронно сохраняет состояние после клика. Если не подождать —
        # последующий клик по другой вкладке может уйти в пустоту, и кнопка
        # «Добавить основное задание» не появится.
        page.wait_for_timeout(800)
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except PWTimeout:
            pass
        # Дополнительная пауза на отрисовку UI после save.
        page.wait_for_timeout(300)
        try:
            return checkbox.first.is_checked()
        except Exception:
            aria = checkbox.first.get_attribute("aria-checked")
            return (aria or "").lower() == "true"

    def _is_button_disabled(self, btn_locator) -> bool:
        """Проверяет несколькими способами, что кнопка disabled (Vue/ExtJS варианты)."""
        try:
            if btn_locator.is_disabled():
                return True
        except Exception:
            pass
        for attr in ("disabled", "aria-disabled"):
            try:
                v = btn_locator.get_attribute(attr)
            except Exception:
                continue
            if v is not None and (attr == "disabled" or (v or "").lower() == "true"):
                return True
        try:
            cls = (btn_locator.get_attribute("class") or "").lower()
            if "is-disabled" in cls or "disabled" in cls:
                return True
        except Exception:
            pass
        return False

    def _dismiss_existing_homework_alert(self) -> bool:
        """Закрывает алерт «Уже добавлено основное задание...» если он появился.

        Возвращает True, если алерт был и был закрыт (значит ДЗ уже есть).
        """
        assert self.page is not None
        page = self.page
        try:
            alert = page.get_by_text(
                re.compile(r"уже\s+добавлено|задание\s+из\s+ктп", re.IGNORECASE)
            )
            if alert.count() == 0 or not alert.first.is_visible():
                return False
        except Exception:
            return False
        # Закрываем — кнопка ОК или крестик модалки.
        for name in ("OK", "ОК", "Ок", "Закрыть"):
            try:
                btn = page.get_by_role("button", name=name, exact=True)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    page.wait_for_timeout(300)
                    return True
            except Exception:
                continue
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return True

    def _find_add_homework_button(self, max_wait_s: float = 8.0):
        """Ищет кнопку «Добавить основное задание» с поллингом до max_wait_s.

        Возвращает (locator_element | None). None означает, что кнопки нет
        (ДЗ уже есть, или редактор показан inline без кнопки, или вкладка
        не загрузилась совсем).
        """
        assert self.page is not None
        page = self.page
        deadline = time.monotonic() + max_wait_s
        while True:
            for loc in (
                page.locator("[role='tabpanel']:visible").last.get_by_role(
                    "button", name="Добавить основное задание", exact=True
                ),
                page.get_by_role("button", name="Добавить основное задание", exact=True),
            ):
                try:
                    if loc.count() > 0 and loc.first.is_visible():
                        return loc.first
                except Exception:
                    pass
            if time.monotonic() >= deadline:
                return None
            try:
                page.wait_for_timeout(300)
            except Exception:
                return None

    def _find_inline_homework_editor(self):
        """Ищет inline-редактор ДЗ ТОЛЬКО по явной метке (без fallback'а на
        случайный contenteditable — иначе можно зацепить редактор темы).

        Возвращает (element | None).
        """
        assert self.page is not None
        page = self.page
        label_re = re.compile(
            r"Основное\s+задание|^Задание\s+на\s+следующий|^Домашнее\s+задание",
            re.IGNORECASE,
        )
        try:
            loc = page.get_by_label(label_re)
            for el in loc.all():
                try:
                    if el.is_visible():
                        return el
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _ensure_tab_active(self, tab_name: str, max_wait_s: float = 5.0) -> bool:
        """Гарантирует, что вкладка с именем tab_name стала активной (aria-selected=true).

        Если БАРС в момент клика занят AJAX-save (после tick'а чекбокса),
        первый клик может проигнорироваться. Делаем до 3 попыток.
        """
        assert self.page is not None
        page = self.page
        tab = page.get_by_role("tab", name=tab_name, exact=True)
        if tab.count() == 0:
            return False
        deadline = time.monotonic() + max_wait_s
        attempts = 0
        while time.monotonic() < deadline:
            try:
                aria = tab.first.get_attribute("aria-selected")
                if aria == "true":
                    return True
            except Exception:
                pass
            if attempts < 3:
                try:
                    tab.first.click()
                    attempts += 1
                except Exception:
                    pass
            page.wait_for_timeout(400)
        return False

    def _log_tab_buttons(self) -> None:
        """Диагностика: что видно на странице, когда кнопка не найдена."""
        assert self.page is not None
        page = self.page
        try:
            buttons = page.get_by_role("button").all()
            visible_texts = []
            for btn in buttons:
                try:
                    if btn.is_visible():
                        visible_texts.append((btn.text_content() or "").strip()[:60])
                except Exception:
                    pass
            self.log(f"    [diag] видимые кнопки: {visible_texts[:15]}")
        except Exception:
            pass
        # Какая вкладка активна?
        try:
            active_tabs = page.locator("[role='tab'][aria-selected='true']:visible").all()
            for t in active_tabs[:3]:
                try:
                    txt = (t.text_content() or "").strip()[:60]
                    self.log(f"    [diag] активная вкладка: {txt!r}")
                except Exception:
                    pass
        except Exception:
            pass
        # Что в активной tabpanel?
        try:
            panel = page.locator("[role='tabpanel']:visible").last
            txt = (panel.text_content() or "").strip().replace("\n", " ")[:200]
            self.log(f"    [diag] текст в tabpanel: {txt!r}")
        except Exception:
            pass
        # Скриншот для оффлайн-диагностики.
        try:
            shot_path = config.PROJECT_ROOT / f"diag_no_button_{int(time.time())}.png"
            page.screenshot(path=str(shot_path), full_page=False)
            self.log(f"    [diag] скриншот сохранён: {shot_path}")
        except Exception as exc:
            self.log(f"    [diag] не удалось сохранить скриншот: {exc}")

    def _add_next_lesson_homework(self, homework: str) -> tuple[str, str]:
        """Переключает на вкладку «Задание на следующий урок» и добавляет ДЗ.

        Две модели UI:
          A. БАРС показывает inline-редактор (contenteditable) сразу — заполняем.
          B. БАРС требует клик «Добавить основное задание» — открываем диалог,
             заполняем, сохраняем дочерний диалог.

        Закрытие модалки урока — снаружи, через _save_and_close_lesson().
        """
        assert self.page is not None
        page = self.page

        if not self._ensure_tab_active("Задание на следующий урок", max_wait_s=8.0):
            self.log("    [diag] не удалось активировать вкладку «Задание на следующий урок»")
            self._log_tab_buttons()
            return "skipped_existing", "вкладка ДЗ не активировалась"
        # Вкладка активна — ждём отрисовку контента.
        page.wait_for_timeout(600)
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except PWTimeout:
            pass

        # Сначала пробуем модель A — inline-редактор. Если в нём уже есть текст,
        # считаем что ДЗ есть; иначе — заполняем.
        inline = self._find_inline_homework_editor()
        if inline is not None:
            try:
                existing = (inline.text_content() or "").strip()
            except Exception:
                existing = ""
            if existing:
                return "skipped_existing", f"ДЗ уже введено inline: {existing[:40]!r}"
            if self.dry_run:
                self.log(f"    [DRY] would fill inline editor: {homework!r}")
                return "filled", f"[DRY] would fill: {homework!r}"
            try:
                inline.click()
                page.wait_for_timeout(150)
                try:
                    inline.evaluate(
                        "el => { "
                        "if ('value' in el) { el.value = ''; } "
                        "else { el.innerHTML = ''; } "
                        "el.dispatchEvent(new InputEvent('input', {bubbles: true})); "
                        "}"
                    )
                except Exception:
                    pass
                page.keyboard.type(homework)
                try:
                    inline.evaluate(
                        "el => el.dispatchEvent(new InputEvent('input', {bubbles: true}))"
                    )
                except Exception:
                    pass
                page.wait_for_timeout(400)
                return "filled", "inline editor"
            except Exception as exc:
                self.log(f"    ! не удалось заполнить inline editor: {exc}")
                # Падаем дальше — может, кнопка «Добавить» доступна.

        # Модель B — ищем кнопку «Добавить основное задание» до 8 секунд.
        add_btn = self._find_add_homework_button()
        if add_btn is None:
            self._log_tab_buttons()
            return "skipped_existing", "ДЗ уже есть (нет ни inline-редактора, ни кнопки)"

        if self._is_button_disabled(add_btn):
            return "skipped_existing", "ДЗ уже есть (кнопка добавления неактивна)"

        if self.dry_run:
            self.log(f"    [DRY] would add homework: {homework!r}")
            return "filled", f"[DRY] would fill: {homework!r}"

        add_btn.click()
        page.wait_for_timeout(500)

        # БАРС мог показать алерт сразу после клика.
        if self._dismiss_existing_homework_alert():
            return "skipped_existing", "БАРС: задание уже добавлено (сразу)"

        # Открылся диалог «Добавление основного задания на следующий урок».
        # Поле — rich-text editor (contenteditable=true), не <input>.
        try:
            self._fill_homework_input(homework)
        except Exception:
            # Алерт мог появиться поверх диалога во время поиска поля.
            if self._dismiss_existing_homework_alert():
                return "skipped_existing", "БАРС: задание уже добавлено (на ввод)"
            raise

        # Сохраняем дочерний диалог.
        if not self._click_dialog_save():
            if self._dismiss_existing_homework_alert():
                return "skipped_existing", "БАРС: задание уже добавлено (перед save)"
            raise RuntimeError("Не нашёл кнопку «Сохранить» в диалоге ввода ДЗ")

        # БАРС может проверить на сервере и асинхронно показать алерт
        # «Уже добавлено» уже ПОСЛЕ нажатия «Сохранить». Подождём и проверим.
        page.wait_for_timeout(800)
        if self._dismiss_existing_homework_alert():
            return "skipped_existing", "БАРС: задание уже добавлено (после save)"

        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except PWTimeout:
            pass
        return "filled", ""

    def _fill_homework_input(self, homework: str) -> None:
        """Заполняет поле основного задания (rich-text editor или textarea).

        В БАРС поле называется «Основное задание на следующий урок» и реализовано
        через contenteditable. Перебираем стратегии: get_by_label по подстроке
        «Основное задание», ближайший к подписи contenteditable, общий
        contenteditable в активном диалоге.
        """
        assert self.page is not None
        page = self.page

        candidates_locators = []
        try:
            candidates_locators.append(
                page.get_by_label(re.compile(r"Основное\s+задание", re.IGNORECASE))
            )
        except Exception:
            pass
        try:
            candidates_locators.append(page.get_by_label("Задание"))
        except Exception:
            pass
        try:
            candidates_locators.append(
                page.locator(
                    "[role='dialog']:visible [contenteditable='true'], "
                    "[class*='dialog']:visible [contenteditable='true'], "
                    "[class*='modal']:visible [contenteditable='true']"
                )
            )
        except Exception:
            pass
        try:
            candidates_locators.append(page.locator("[contenteditable='true']:visible"))
        except Exception:
            pass

        for loc in candidates_locators:
            try:
                if loc.count() == 0:
                    continue
            except Exception:
                continue
            for el in loc.all():
                try:
                    if not el.is_visible():
                        continue
                    tag = (el.evaluate("el => el.tagName") or "").lower()
                    editable_raw = (el.get_attribute("contenteditable") or "").lower()
                    is_editable = editable_raw in ("true", "", "plaintext-only")
                    if tag in ("input", "textarea"):
                        el.fill(homework)
                        return
                    if is_editable:
                        el.click()
                        page.wait_for_timeout(150)
                        # Очищаем + сообщаем Vue об изменении через input-событие.
                        try:
                            el.evaluate(
                                "el => { el.innerHTML = ''; "
                                "el.dispatchEvent(new InputEvent('input', {bubbles: true})); }"
                            )
                        except Exception:
                            pass
                        page.keyboard.type(homework)
                        # ещё одно input-событие после печати
                        try:
                            el.evaluate(
                                "el => el.dispatchEvent(new InputEvent('input', {bubbles: true}))"
                            )
                        except Exception:
                            pass
                        return
                except Exception:
                    continue

        raise RuntimeError("Не нашёл поле ввода «Основное задание»")

    def _click_dialog_save(self) -> bool:
        """Нажимает кнопку «Сохранить» в активном диалоге (а не в модалке урока)."""
        assert self.page is not None
        page = self.page
        # Скоупим в видимом диалоге, чтобы не задеть «Сохранить» вне его.
        scopes = [
            "[role='dialog']:visible",
            "[class*='dialog']:visible",
            "[class*='modal']:visible",
        ]
        for scope_sel in scopes:
            try:
                scope = page.locator(scope_sel).last
                if scope.count() == 0:
                    continue
                btn = scope.get_by_role(
                    "button", name=re.compile(r"^(Сохранить|ОК|Ок|Применить)$")
                )
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    return True
            except Exception:
                continue
        # Fallback: глобальный поиск (берём первую видимую)
        try:
            btn = page.get_by_role(
                "button", name=re.compile(r"^(Сохранить|ОК|Ок|Применить)$")
            )
            for el in btn.all():
                try:
                    if el.is_visible():
                        el.click()
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _save_and_close_lesson(self) -> bool:
        """Нажимает «Сохранить и закрыть урок» в модалке.

        Кнопка появляется в БАРС только когда галочка «Урок проведен» стоит,
        но после tick'а / save'а дочернего диалога ДЗ может появиться с
        задержкой 100-1000ms — поэтому поллим до 3 секунд.
        """
        assert self.page is not None
        page = self.page
        if self.dry_run:
            # В dry_run логически save не выполняем, но модалку всё равно надо
            # физически закрыть — иначе следующая колонка не откроется
            # (overlay <article class="window"> перехватывает клики).
            self.log("    [DRY] would click «Сохранить и закрыть урок»")
            self._close_lesson_modal()
            return True

        pattern = re.compile(r"сохранить.*и.*закры", re.IGNORECASE)
        deadline = time.monotonic() + 3.0
        btn_first = None
        while time.monotonic() < deadline:
            try:
                btn = page.get_by_role("button", name=pattern)
                if btn.count() > 0 and btn.first.is_visible():
                    btn_first = btn.first
                    break
            except Exception:
                pass
            page.wait_for_timeout(200)
        if btn_first is None:
            return False
        try:
            btn_first.click()
        except Exception as exc:
            self.log(f"    ! не удалось «Сохранить и закрыть»: {exc}")
            return False
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except PWTimeout:
            pass
        return True

    def _is_lesson_modal_open(self) -> bool:
        """Видна ли сейчас модалка «Журнал на урок»."""
        assert self.page is not None
        try:
            tab = self.page.get_by_role("tab", name="Урок", exact=True)
            return tab.count() > 0 and tab.first.is_visible()
        except Exception:
            return False

    def _close_lesson_modal(self) -> None:
        """Закрывает модалку «Журнал на урок» без сохранения.

        В БАРС модалка — `<article class="window windowPage window_maximized ...">`.
        Крестик закрытия — обычно по `aria-label="Закрыть"` или CSS `*close*`
        внутри этого `<article>`. Перебираем стратегии и проверяем по
        `_is_lesson_modal_open`, что закрылось. Используем force/JS-click как
        фоллбэк, если обычный click перехватывается overlay'ями.
        """
        assert self.page is not None
        page = self.page

        if not self._is_lesson_modal_open():
            return

        strategies = [
            # 1. role=button «Закрыть»/«Отмена»
            lambda: page.get_by_role("button", name="Закрыть"),
            lambda: page.get_by_role("button", name="Отмена"),
            # 2. крестик внутри активного <article class="window">
            lambda: page.locator(
                "article.window:visible [aria-label='Закрыть'], "
                "article[class*='window']:visible [aria-label='Закрыть']"
            ),
            lambda: page.locator(
                "article.window:visible [class*='close']:visible, "
                "article[class*='window']:visible [class*='close']:visible"
            ),
            # 3. крестик по aria-label, видимый — где угодно
            lambda: page.locator("[aria-label='Закрыть']:visible"),
            # 4. ExtJS-style tool-close
            lambda: page.locator(
                ".x-tool-close:visible, [class*='tool-close']:visible, "
                "[class*='close-icon']:visible"
            ),
            # 5. крестик внутри dialog/modal/window
            lambda: page.locator(
                "[role='dialog']:visible [class*='close']:visible, "
                "[class*='modal']:visible [class*='close']:visible, "
                "[class*='window']:visible [class*='close']:visible"
            ),
            # 6. крестик у вкладки «Журнал на урок» внизу
            lambda: page.locator(
                "[role='tab']:has-text('Журнал на урок') [aria-label='Закрыть'], "
                "[role='tab']:has-text('Журнал на урок') [class*='close']"
            ),
        ]

        def _try_click(el) -> bool:
            """Пытается кликнуть тремя способами; возвращает True если хоть один прошёл."""
            for click_kind in ("normal", "force", "js"):
                try:
                    if click_kind == "normal":
                        el.click(timeout=2_000)
                    elif click_kind == "force":
                        el.click(force=True, timeout=2_000)
                    else:  # js
                        el.evaluate("el => el.click()")
                    return True
                except Exception:
                    continue
            return False

        for build in strategies:
            try:
                loc = build()
                if loc.count() == 0:
                    continue
                for el in loc.all():
                    try:
                        if not el.is_visible():
                            continue
                    except Exception:
                        continue
                    if _try_click(el):
                        page.wait_for_timeout(400)
                        if not self._is_lesson_modal_open():
                            return
            except Exception:
                continue

        # fallback — несколько Escape
        for _ in range(3):
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(250)
                if not self._is_lesson_modal_open():
                    return
            except Exception:
                pass

        if self._is_lesson_modal_open():
            self.log("    ! модалка не закрылась ни одной стратегией")
