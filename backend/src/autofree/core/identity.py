"""注册时用的随机身份(姓名/生日/密码)。从 autoteam.identity 抽出来精简版,不依赖 autoteam。"""

from __future__ import annotations

import random
import string

_FIRST_NAMES = (
    "Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Avery",
    "Quinn", "Cameron", "Reese", "Skyler", "Emma", "Olivia", "Sophia", "Mia",
    "Isabella", "Charlotte", "Amelia", "Harper", "Liam", "Noah", "Oliver", "Ethan",
    "James", "Benjamin", "Lucas", "Mason", "William", "Henry",
)
_LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
)
_PASSWORD_WORDS = (
    "Harbor", "Forest", "River", "Mountain", "Ocean", "Valley", "Canyon", "Meadow",
    "Sunset", "Dawn", "Storm", "Thunder", "Crystal", "Marble", "Granite", "Willow",
    "Maple", "Cedar", "Birch", "Aspen", "Falcon", "Eagle", "Phoenix", "Dragon",
)


def random_full_name() -> str:
    return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"


def random_password() -> str:
    """≥12 字符;OpenAI signup 校验密码长度 >= 12。"""
    word1 = random.choice(_PASSWORD_WORDS)
    word2 = random.choice(_PASSWORD_WORDS).lower()
    digits = "".join(random.choices(string.digits, k=random.choice([3, 3, 4])))
    symbol = random.choice("!@#$")
    return f"{word1}{word2}{digits}{symbol}"


def random_birthday(min_age: int = 22, max_age: int = 42) -> dict[str, str]:
    """{year, month, day},month/day 补零到两位。避开 18-21(OpenAI 风控较严)。"""
    import datetime as _dt

    today = _dt.date.today()
    age = random.randint(min_age, max_age)
    return {
        "year": str(today.year - age),
        "month": f"{random.randint(1, 12):02d}",
        "day": f"{random.randint(1, 28):02d}",
    }


def random_email_prefix(length: int = 10) -> str:
    """全小写字母+数字,不含点/下划线 — cf_temp_email 兼容。"""
    pool = string.ascii_lowercase + string.digits
    return "".join(random.choices(pool, k=length))
