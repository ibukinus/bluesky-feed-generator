import re
import tomllib
from sudachipy import dictionary

tokenizer = dictionary.Dictionary().create()

with open("keyword.toml", mode="rb") as f:
    keyword: dict[str, list[str]] = tomllib.load(f)

rank1: list[str] = []
rank2: list[str] = []
if "rank1" in keyword:
    rank1 = keyword.get("rank1")
if "rank2" in keyword:
    rank2 = keyword.get("rank2")

rank1_regex = re.compile("^" + "$|^".join(rank1) + "$", re.IGNORECASE)
rank2_regex = re.compile("^" + "$|^".join(rank2) + "$", re.IGNORECASE)

def match_shiny_colors(text: str) -> bool:
    """与えられた文章にシャニマスに関連するワードが含まれるか検査します。
    
    Args:
        text (str): 検査対象のテキスト
    
    Returns:
        bool: シャニマスに関連するワードが含まれる場合はTrue
    """
    tokens = tokenizer.tokenize(text)
    rank2_match_word = set()
    for token in tokens:
        if rank1_regex.match(token.normalized_form()):
            return True
        rank2_result = rank2_regex.findall(token.normalized_form())
        if rank2_result:
            rank2_match_word |= set(rank2_result)
    return len(rank2_match_word) > 1
