import re
from typing import Iterable, List, Set


def normalize_skill(skill: str) -> str:
    return skill.strip().lower().replace(" ", "_")


def tokenize(text: str) -> List[str]:
    text = text.lower()
    return re.findall(r"[a-z0-9_+#.]+|[\u4e00-\u9fff]{2,}", text)


def overlap_score(left: Iterable[str], right: Iterable[str]) -> float:
    left_set: Set[str] = {item for item in left if item}
    right_set: Set[str] = {item for item in right if item}
    if not left_set or not right_set:
        return 0.0
    return len(left_set.intersection(right_set)) / len(left_set.union(right_set))

