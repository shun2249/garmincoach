# 論文清單

`papers.tsv` 是教練知識庫收錄的論文書目（PMID、PMCID、DOI、年份、證據等級、是否有開放全文、主題、期刊、標題），由 `python -m garmin_coach kb manifest` 從 `data/knowledge.db` 產生。只有書目，沒有摘要或全文。

- 想用和作者一樣的知識庫：`kb download`（下載預建好的向量庫，約 70 MB）或 `kb build --manifest`（照這份清單重抓同一批論文，自己向量化）。
- `evidence`：meta（統合分析）/ sysrev（系統性回顧）/ rct / review / other。
- `fulltext`：ok（有 Europe PMC 開放全文）/ none（沒有開放全文，重建時不再試）/ error（上次抓取失敗，重建時會再試）/ 空白（未嘗試）。
