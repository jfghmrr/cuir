from pathlib import Path

BARS_URL = "https://es.ciur.ru/"
PROJECT_ROOT = Path(__file__).parent
# Постоянный профиль Chromium: cookies, localStorage и т.п. сохраняются между запусками,
# поэтому через Госуслуги логинимся только в первый раз (и пока сессия не протухнет).
PROFILE_DIR = PROJECT_ROOT / "chrome_profile"

NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 15_000
