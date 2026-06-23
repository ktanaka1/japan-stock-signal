"""TDnet開示の銘柄同定を権威データ(security_code/security_name)で確定するテスト。"""
from monitor.agent import _normalize_security_code, _reconcile_identity


class _Art:
    def __init__(self, security_code=None, security_name=None):
        self.security_code = security_code
        self.security_name = security_name


def test_normalize_5digit_to_4():
    assert _normalize_security_code("65330") == "6533"
    assert _normalize_security_code("79860") == "7986"
    assert _normalize_security_code("587A4") == "587A"  # 新形式英数


def test_normalize_keeps_4digit():
    assert _normalize_security_code("6533") == "6533"
    assert _normalize_security_code("376A") == "376A"


def test_normalize_invalid():
    assert _normalize_security_code("") is None
    assert _normalize_security_code(None) is None
    assert _normalize_security_code("12") is None


def test_overrides_hallucinated_name():
    """コードは正しいが社名が捏造されたケース（6533をベイカレントと誤記）を権威社名で上書き。"""
    art = _Art(security_code="65330", security_name="Ｏｒｃｈｅｓｔｒａ　Ｈｏｌｄｉｎｇｓ")
    stocks = [{"name": "ベイカレント・コンサルティング", "code": "6533"}]
    out = _reconcile_identity(art, stocks)
    assert out == [{"name": "Ｏｒｃｈｅｓｔｒａ　Ｈｏｌｄｉｎｇｓ", "code": "6533"}]


def test_adds_missing_disclosing_company():
    """LLMが開示会社のコードを挙げていない場合は権威データを先頭に補う。"""
    art = _Art(security_code="79860", security_name="日本アイエスケイ")
    stocks = []
    out = _reconcile_identity(art, stocks)
    assert out == [{"name": "日本アイエスケイ", "code": "7986"}]


def test_keeps_other_related_stocks():
    """開示会社を確定しつつ、LLMが挙げた他の関連銘柄は残す。"""
    art = _Art(security_code="79860", security_name="日本アイエスケイ")
    stocks = [{"name": "関連会社", "code": "1234"}]
    out = _reconcile_identity(art, stocks)
    assert {"name": "日本アイエスケイ", "code": "7986"} in out
    assert {"name": "関連会社", "code": "1234"} in out


def test_no_authority_is_noop():
    """RSS由来など権威情報が無ければ何もしない（LLM結果のまま）。"""
    art = _Art(security_code=None, security_name=None)
    stocks = [{"name": "どこかの会社", "code": "9999"}]
    out = _reconcile_identity(art, stocks)
    assert out == [{"name": "どこかの会社", "code": "9999"}]


def test_code_without_name_is_noop():
    """社名が無ければ上書きしない（誤った空名で確定しない）。"""
    art = _Art(security_code="65330", security_name=None)
    stocks = [{"name": "ベイカレント", "code": "6533"}]
    out = _reconcile_identity(art, stocks)
    assert out == [{"name": "ベイカレント", "code": "6533"}]
