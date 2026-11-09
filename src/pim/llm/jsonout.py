import json


def extract_json(raw: str) -> dict:
    """JSON-объект из ответа модели: модели любят обрамлять его текстом или ```json — вырезаем фигурные скобки."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("в ответе нет JSON")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON — не объект")
    return data
