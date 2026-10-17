"""所見・鑑別診断などのプロンプト定義。"""
from __future__ import annotations

SYSTEM_PROMPT = (
    "あなたは病理診断を支援するAIアシスタントです。病理医の所見作成や鑑別診断の検討を支援します。"
    "断定的な診断を下すのではなく、根拠とともに参考情報として提示してください。"
    "確認すべき追加検査・免疫染色・臨床情報があれば併記してください。"
    "画像を添付された場合のみ画像を根拠にでき、添付がない場合は提供された情報と一般論に基づいて検討してください。"
)

VIEWER_NOTE = (
    "※このアプリで表示中の画像は、ユーザーが「画像を添付」を有効にした場合のみ送信されます。"
)


def case_info_block(case_info: str) -> str:
    return f"# 標本情報\n{case_info}\n"


def notes_block(notes: str) -> str:
    return f"# ユーザーメモ\n{notes if notes.strip() else '(なし)'}\n"


def findings_prompt(case_info: str, notes: str) -> str:
    return (
        case_info_block(case_info)
        + "\n"
        + notes_block(notes)
        + "\nこの標本について、気づいた所見を箇条書きで列挙してください。"
        "各項目に簡単な根拠を添え、最後に総括コメントを1〜2文で述べてください。"
    )


def differential_prompt(case_info: str, notes: str) -> str:
    return (
        case_info_block(case_info)
        + "\n"
        + notes_block(notes)
        + "\nこの症例の鑑別診断を、可能性が高いと思われる順に列挙してください。"
        "各候補について (1) 根拠、(2) 確定・否定に向けた追加検査や免疫染色マーカーの候補、を簡潔にまとめてください。"
        "最後に「重要な鑑別の見落とし回避ポイント」を1つ挙げてください。"
    )
