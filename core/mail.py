import re
from datetime import datetime
from math import isfinite
from core.models import MailLetter


def parse_letter_date(date: int | float | str) -> float:
    if isinstance(date, str):
        try:
            date = float(date)
        except ValueError:
            result = datetime.fromisoformat(date.replace("Z", "+00:00"))

            if result.tzinfo is None:
                raise ValueError("Letter date must include a timezone")

            return result.timestamp()

    if not isinstance(date, (int, float)) or isinstance(date, bool):
        raise ValueError("Invalid letter date")

    if not isfinite(date):
        raise ValueError("Invalid letter date")

    if abs(date) > 20_000_000_000:
        date = date / 1000

    return float(date)


def extract_verification_code(text: str) -> str | None:
    match = re.search(
        r"\b(?:verification\s+code|security\s+code|code|код)\b\D{0,30}(?<!\d)([0-9]{6})(?!\d)",
        text,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    codes = re.finditer(r"(?<!\d)[0-9]{6}(?!\d)", text)
    first_code = next(codes, None)

    if first_code and next(codes, None) is None:
        return first_code.group()

    return None


def get_latest_letter(letters: list[MailLetter]) -> MailLetter | None:
    if not letters:
        return None

    latest_letter = max(letters, key=lambda letter: letter.date)

    return latest_letter


def get_verification_code(
    letters: list[MailLetter],
    sender: str = "",
    subject: str = "",
    after_date: float | None = None,
    ignored_ids: set[str] | None = None,
) -> str | None:
    sender = sender.casefold()
    subject = subject.casefold()
    matching_letters = []
    latest_letter = None

    for letter in letters:
        if sender and letter.sender.casefold() != sender:
            continue

        if subject and subject not in letter.subject.casefold():
            continue

        if after_date is not None and letter.date <= after_date:
            continue

        if ignored_ids and letter.id in ignored_ids:
            continue

        matching_letters.append(letter)
        if latest_letter is None or letter.date > latest_letter.date:
            latest_letter = letter

    if latest_letter is None:
        return None

    code = extract_verification_code(latest_letter.text)
    if code:
        return code

    matching_letters.sort(key=lambda letter: letter.date, reverse=True)

    for letter in matching_letters:
        if letter is latest_letter:
            continue

        code = extract_verification_code(letter.text)

        if code:
            return code

    return None
