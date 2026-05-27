from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd


HEADER_ROW_MARKER = "Число и месяц"
SUBJECT_ROW = 0
SUBJECT_COL = 3
GROUP_ROW = 4
GROUP_COL = 1
TEACHER_ROW = 50
TEACHER_COL = 3
DATE_COL = 0
TOPIC_COL = 2
HOMEWORK_COL = 17

CLASS_FROM_FILENAME_RE = re.compile(r"(\d+)\s*([А-Яа-яЁё])")
GROUP_FROM_HEADER_RE = re.compile(r'группы\s*"([^"]+)"', re.IGNORECASE)


@dataclass
class Lesson:
    date_hint: str
    topic: str
    homework: Optional[str]


RUSSIAN_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}


def _guess_period(lessons: list["Lesson"]) -> str:
    """По первой дате урока угадывает полугодие БАРС."""
    if not lessons:
        return "2 Полугодие"
    first = (lessons[0].date_hint or "").lower()
    for stem, num in RUSSIAN_MONTHS.items():
        if stem in first:
            return "1 Полугодие" if num >= 9 else "2 Полугодие"
    return "2 Полугодие"


@dataclass
class GroupSchedule:
    klass: str
    subject: str
    group: str
    teacher: str
    period: str = "2 Полугодие"
    lessons: list[Lesson] = field(default_factory=list)

    def fillable(self) -> list[Lesson]:
        return [l for l in self.lessons if l.homework is not None and l.homework != ""]


def _clean(value) -> str:
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _parse_class_from_filename(path: Path) -> str:
    m = CLASS_FROM_FILENAME_RE.search(path.stem)
    if not m:
        raise ValueError(
            f"Не удалось определить класс из имени файла: {path.name}. "
            "Ожидался формат вида '9а-1.xls' или '7Б.xls'."
        )
    return f"{m.group(1)} {m.group(2).upper()}"


def _parse_group(df: pd.DataFrame) -> str:
    raw = _clean(df.iat[GROUP_ROW, GROUP_COL])
    m = GROUP_FROM_HEADER_RE.search(raw)
    if not m:
        raise ValueError(
            f"Не удалось извлечь имя группы из ячейки [{GROUP_ROW},{GROUP_COL}]: {raw!r}"
        )
    return m.group(1).strip()


def _find_homework_tables(df: pd.DataFrame) -> list[int]:
    """Все строки-заголовки «Число и месяц». В файле БАРС бывает несколько
    блоков (по одному на каждую часть полугодия / страницу журнала)."""
    rows = [
        idx for idx in range(len(df))
        if _clean(df.iat[idx, DATE_COL]) == HEADER_ROW_MARKER
    ]
    if not rows:
        raise ValueError(f"В файле не найдена строка-заголовок «{HEADER_ROW_MARKER}»")
    return rows


def _parse_lessons(
    df: pd.DataFrame, header_row: int, end_row: int | None = None
) -> list[Lesson]:
    """Парсит блок уроков от строки header_row+1 до end_row (или конца файла).

    Останавливается после трёх пустых строк подряд (а не одной — между разделами
    глав в журналах часто бывает один пустой ряд-разделитель).
    """
    end = end_row if end_row is not None else len(df)
    lessons: list[Lesson] = []
    blanks = 0
    for r in range(header_row + 1, end):
        date_hint = _clean(df.iat[r, DATE_COL])
        topic = _clean(df.iat[r, TOPIC_COL])
        if not date_hint and not topic:
            blanks += 1
            if blanks >= 3:
                break
            continue
        blanks = 0
        if not topic:
            continue
        raw_hw = df.iat[r, HOMEWORK_COL]
        homework: Optional[str]
        if pd.isna(raw_hw):
            homework = None
        else:
            cleaned = _clean(raw_hw)
            # "нет", "—", "-", "нет задания" и т.п. = ДЗ не задано.
            if not cleaned or re.match(
                r"^(нет(\s+дз|\s+задания)?|no|none|[-–—])$",
                cleaned, re.IGNORECASE
            ):
                homework = None
            else:
                homework = cleaned
        lessons.append(Lesson(date_hint=date_hint, topic=topic, homework=homework))
    return lessons


def parse(path: str | Path) -> GroupSchedule:
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".xls":
        engine = "xlrd"
    elif ext == ".xlsx":
        engine = "openpyxl"
    else:
        raise ValueError(
            f"Неподдерживаемый формат файла: {ext!r}. Ожидается .xls или .xlsx."
        )
    df = pd.read_excel(path, sheet_name=0, header=None, engine=engine)

    schedule = GroupSchedule(
        klass=_parse_class_from_filename(path),
        subject=_clean(df.iat[SUBJECT_ROW, SUBJECT_COL]),
        group=_parse_group(df),
        teacher=_clean(df.iat[TEACHER_ROW, TEACHER_COL]),
    )
    headers = _find_homework_tables(df)
    all_lessons: list[Lesson] = []
    for i, header_row in enumerate(headers):
        next_header = headers[i + 1] if i + 1 < len(headers) else None
        all_lessons.extend(_parse_lessons(df, header_row, end_row=next_header))
    schedule.lessons = all_lessons
    schedule.period = _guess_period(schedule.lessons)
    return schedule


if __name__ == "__main__":
    import sys

    src = sys.argv[1] if len(sys.argv) > 1 else "/Users/jfghmrr/Downloads/9а-1.xls"
    s = parse(src)
    print(f"Класс:   {s.klass}")
    print(f"Предмет: {s.subject}")
    print(f"Группа:  {s.group}")
    print(f"Учитель: {s.teacher}")
    print(f"Уроков всего:        {len(s.lessons)}")
    print(f"Уроков с ДЗ к заливке: {len(s.fillable())}")
    print()
    for l in s.lessons:
        marker = "·" if l.homework else " "
        hw = l.homework or "—"
        print(f"  {marker} {l.date_hint:>14}  | {l.topic[:60]:<60} | {hw}")
