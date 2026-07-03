#!/usr/bin/env python3
"""JPX 上場会社一覧（data_j.xls）から data/listed_companies.tsv を再生成するオフラインスクリプト。

PR TIMES など証券コードを持たない情報源の名寄せ辞書のメンテ用。**手元で随時（月次目安）実行**し、
生成された TSV をコミットする。新規上場/社名変更へのメンテ漏れは「取りこぼし」に留まり事故化しない。

重い Excel 依存（xlrd）はこのスクリプト専用で、collector/Render/Actions のデプロイ依存には
混ぜない（requirements.txt に追加しない）。実行前に手元の venv にだけ入れること:

    .venv/bin/pip install xlrd          # data_j.xls は BIFF(.xls) なので xlrd を使う

使い方:
    .venv/bin/python scripts/refresh_listed_companies.py
    .venv/bin/python scripts/refresh_listed_companies.py --xls /path/to/data_j.xls   # 取得済みを変換

出力: data/listed_companies.tsv（ヘッダ "code<TAB>name"・コード昇順）。
内国株式（プライム/スタンダード/グロース）のみを採用し、ETF/ETN/REIT/外国株式/PRO Market は除外する。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_JPX_XLS_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/"
    "tvdivq0000001vg2-att/data_j.xls"
)
_OUT_PATH = Path(__file__).parent.parent / "data" / "listed_companies.tsv"

# 採用する市場区分（内国株式のみ）。ETF/ETN/REIT/外国株式/PRO Market/出資証券は名寄せ対象外。
_ACCEPT_MARKETS = {
    "プライム（内国株式）",
    "スタンダード（内国株式）",
    "グロース（内国株式）",
}


def _download(url: str) -> bytes:
    import httpx  # 既存依存

    headers = {"User-Agent": "Mozilla/5.0 (compatible; japan-stock-signal/1.0; +refresh)"}
    resp = httpx.get(url, headers=headers, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def _parse(xls_bytes: bytes) -> list[tuple[str, str]]:
    try:
        import xlrd  # スクリプト専用依存（requirements には入れない）
    except ImportError:
        sys.exit("xlrd が必要です: .venv/bin/pip install xlrd")

    wb = xlrd.open_workbook(file_contents=xls_bytes)
    sh = wb.sheet_by_index(0)
    header = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
    try:
        i_code = header.index("コード")
        i_name = header.index("銘柄名")
        i_market = header.index("市場・商品区分")
    except ValueError:
        sys.exit(f"想定外のヘッダです: {header}")

    rows: list[tuple[str, str]] = []
    for r in range(1, sh.nrows):
        market = str(sh.cell_value(r, i_market)).strip()
        if market not in _ACCEPT_MARKETS:
            continue
        raw_code = sh.cell_value(r, i_code)
        # 数値セル(1301.0) も文字列セルも 4桁文字列に正規化
        if isinstance(raw_code, float):
            code = str(int(raw_code))
        else:
            code = str(raw_code).strip()
        name = str(sh.cell_value(r, i_name)).strip()
        if not code or not name:
            continue
        # 普通株は4桁（新形式 "130A" 含む）。5桁は優先株・社債型種類株式
        # （例: 25935 伊藤園第１種優先株式）で名寄せ対象外なので除外する。
        if len(code) != 4:
            continue
        rows.append((code, name))
    rows.sort(key=lambda x: x[0])
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xls", help="ローカルの data_j.xls パス（省略時は JPX から取得）")
    ap.add_argument("--out", default=str(_OUT_PATH), help="出力 TSV パス")
    args = ap.parse_args()

    if args.xls:
        xls_bytes = Path(args.xls).read_bytes()
    else:
        print(f"Downloading {_JPX_XLS_URL}")
        xls_bytes = _download(_JPX_XLS_URL)

    rows = _parse(xls_bytes)
    if not rows:
        sys.exit("採用行が0件でした。フォーマット変更の可能性があります。")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        f.write("code\tname\n")
        for code, name in rows:
            f.write(f"{code}\t{name}\n")
    print(f"Wrote {len(rows)} companies to {out}")


if __name__ == "__main__":
    main()
