"""PR TIMES 収集（Phase1）のテスト。すべてオフライン（feedparser はサンプルRDFをモック）。

検証観点:
  - 上場社名は権威コード付きで Article 化、非上場は破棄
  - 社名正規化（株式会社/(株)/㈱/HD/全半角/空白）の吸収で突合できる
  - 辞書ローダ（TSVロード・曖昧キー除外・未一致時に安全に破棄）
  - prtimes_enabled="false" で完全 no-op
"""
import textwrap
from pathlib import Path

import feedparser

from store import listed_companies as lc
from store import settings as cfg
from store.db import migrate


# --- サンプル辞書（上場4社）。全半角・HD表記を混在させて正規化の手本にする ---
_SAMPLE_TSV = textwrap.dedent(
    """\
    code\tname
    7419\tノジマ
    9425\tＲｅＹｕｕ　Ｊａｐａｎ
    2685\tアンドエスティＨＤ
    3382\tセブン＆アイ・ホールディングス
    """
)


def _write_tsv(tmp_path: Path) -> Path:
    p = tmp_path / "listed.tsv"
    p.write_text(_SAMPLE_TSV, encoding="utf-8")
    return p


def _load_sample(tmp_path):
    lc.load_from(_write_tsv(tmp_path))


# サンプルRDF: 上場(ノジマ/ReYuu)＋非上場(架空)＋(株)表記の上場分を混在
_SAMPLE_RDF = textwrap.dedent(
    """\
    <?xml version="1.0" encoding="UTF-8"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns="http://purl.org/rss/1.0/"
      xmlns:dc="http://purl.org/dc/elements/1.1/">
    <item rdf:about="https://prtimes.jp/p/1.html">
      <title>ノジマ、人気IPとコラボ開始</title>
      <link>https://prtimes.jp/p/1.html</link>
      <dc:corp>株式会社ノジマ</dc:corp>
    </item>
    <item rdf:about="https://prtimes.jp/p/2.html">
      <title>架空スタートアップが資金調達</title>
      <link>https://prtimes.jp/p/2.html</link>
      <dc:corp>株式会社架空スタートアップ</dc:corp>
    </item>
    <item rdf:about="https://prtimes.jp/p/3.html">
      <title>ReYuu Japan、新サービス</title>
      <link>https://prtimes.jp/p/3.html</link>
      <dc:corp>ReYuu Japan株式会社</dc:corp>
    </item>
    <item rdf:about="https://prtimes.jp/p/4.html">
      <title>corp欠落のPR</title>
      <link>https://prtimes.jp/p/4.html</link>
    </item>
    </rdf:RDF>
    """
)


def _patch_feedparser(monkeypatch, rdf: str = _SAMPLE_RDF):
    """feedparser.parse を URL 無視でサンプルRDFを返すスタブに差し替える。"""
    from collector import fetcher
    parsed = feedparser.parse(rdf)  # 文字列パースはネットワーク非依存
    monkeypatch.setattr(fetcher.feedparser, "parse", lambda url: parsed)


# ---------------- 名寄せ（正規化）ユニット ----------------

def test_normalize_absorbs_corp_and_width(tmp_path):
    _load_sample(tmp_path)
    # 株式会社（前置）、(株)、㈱、全半角・空白、HD↔ホールディングス を吸収して一致
    assert lc.lookup("株式会社ノジマ") == ("7419", "ノジマ")
    assert lc.lookup("ノジマ(株)") == ("7419", "ノジマ")
    assert lc.lookup("㈱ノジマ") == ("7419", "ノジマ")
    assert lc.lookup("ＲｅＹｕｕ　Ｊａｐａｎ株式会社") == ("9425", "ＲｅＹｕｕ　Ｊａｐａｎ")
    assert lc.lookup("ReYuu Japan株式会社") == ("9425", "ＲｅＹｕｕ　Ｊａｐａｎ")
    # JPX は "ＨＤ"、PR TIMES は "ホールディングス" でも突合できる
    assert lc.lookup("アンドエスティホールディングス") == ("2685", "アンドエスティＨＤ")
    assert lc.lookup("セブン＆アイＨＤ") == ("3382", "セブン＆アイ・ホールディングス")


def test_lookup_unmatched_returns_none(tmp_path):
    _load_sample(tmp_path)
    assert lc.lookup("株式会社架空スタートアップ") is None
    assert lc.lookup("") is None
    assert lc.lookup("   ") is None


# ---------------- 辞書ローダ ----------------

def test_loader_drops_ambiguous_keys(tmp_path):
    # 同じ正規化キーに別コードが衝突 → 曖昧として両方除外（誤同定回避）
    p = tmp_path / "amb.tsv"
    p.write_text("code\tname\n1111\tかぶ\n2222\t株式会社かぶ\n", encoding="utf-8")
    lc.load_from(p)
    assert lc.lookup("かぶ") is None  # 衝突キーは安全に破棄


def test_loader_missing_file_disables_lookups(tmp_path):
    lc.load_from(tmp_path / "nope.tsv")
    assert lc.lookup("株式会社ノジマ") is None


# ---------------- 収集（_fetch_prtimes） ----------------

def test_fetch_prtimes_collects_only_listed(tmp_path, monkeypatch):
    migrate()
    _load_sample(tmp_path)
    cfg.set_value("prtimes_enabled", "true")
    cfg.set_value("prtimes_rss_url", "https://prtimes.jp/index.rdf")
    _patch_feedparser(monkeypatch)

    from collector import fetcher
    arts = fetcher._fetch_prtimes()

    # 上場2社（ノジマ・ReYuu）のみ。非上場・corp欠落は破棄
    codes = sorted(a.security_code for a in arts)
    assert codes == ["7419", "9425"]
    nojima = next(a for a in arts if a.security_code == "7419")
    assert nojima.security_name == "ノジマ"
    assert nojima.title == "ノジマ、人気IPとコラボ開始"
    assert nojima.body == nojima.title  # body はタイトルのみ
    assert nojima.url == "https://prtimes.jp/p/1.html"


def test_fetch_prtimes_noop_when_disabled(tmp_path, monkeypatch):
    migrate()
    _load_sample(tmp_path)
    cfg.set_value("prtimes_enabled", "false")
    # 無効時は feedparser.parse を呼ばない（呼んだら失敗させて検出）
    from collector import fetcher
    monkeypatch.setattr(fetcher.feedparser, "parse",
                        lambda url: (_ for _ in ()).throw(AssertionError("must not fetch")))
    assert fetcher._fetch_prtimes() == []
