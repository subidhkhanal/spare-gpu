# What empirical reductions in hallucination have been measured for retrieval-augmented language models compared with closed-book LLMs, on named benchmarks and datasets?

## In brief
- Biggest headline effect: knowledge-grounded dialogue. On Wizard of Wikipedia (human eval), BART-Large closed-book had a 68.2% hallucination rate; retrieval-augmented (FiD-RAG/RAG-Sequence, 5 docs) cut it to 7.9–9.6%, with knowledgeability rising 34.1% → 94.9–96.1% [shuster2021]. Effect is >60% relative reduction, larger out-of-distribution.
- Open-domain QA gains are real but modest: GPT-3 (16-shot, EM) went NQ 40.6 → 44.2, TriviaQA 73.6 → 76.0, SQuAD 20.2 → 34.0 with Contriever top-10 passages [wang2023]. Retrieval gains concentrate on long-tail/popularity-poor facts: Contriever gave GPT-3 davinci-003 +7% accuracy on PopQA [mallen2023].
- Long-form citation generation (ALCE: ASQA/QAMPARI/ELI5): ChatGPT closed-book vs vanilla RAG (5 passages) — correctness 38.3 vs 40.4 on ASQA, but citation recall/precision collapsed closed-book (26.7/26.7 vs 73.6/72.5) [gao2023]. Correctness gains are small; the large measured effect is attribution/faithfulness.
- Medical QA (MIRAGE, 7,663 questions): MEDRAG vs CoT (closed-book) lifted GPT-4 avg 73.44 → 79.97, GPT-3.5 60.69 → 71.57; biggest per-dataset jump PubMedQA* 36.00 → 67.40 (+31.4) for GPT-3.5 [xiong2024].
- Cases where retrieval did not help or hurt: MMLU-Med accuracy slightly dropped with RAG for GPT-4 (93.1 → 92.5) [xiong2024]; CRAG benchmark found straightforward RAG raised GPT-4 Turbo accuracy 34% → 44% but no RAG solution exceeded 20% truthfulness because irrelevant retrievals added hallucinations [cuconasu2024]; RAG with misleading/irrelevant contexts fell below zero-shot baselines [zhang2025-raguard].

## Findings

**Dialogue: largest measured hallucination drop (human eval).**
- "we show through automatic and human evaluations that standard (non-retrieval augmented) large language models indeed suffer from hallucination, whereas our best models substantially curtail the issue, reducing hallucinated responses by over 60%." [shuster2021] — headline for knowledge-grounded dialogue (WoW, CMU_DoG).
- Table (WoW test seen, human eval): "BART-Large ... Knowledgeable 34.1% ... Hallucination 68.2%" vs "FiD-RAG | 5 docs ... 96.1% | 7.9%" and "RAG-Sequence | 5 docs ... 94.9% | 9.6%". [shuster2021] — same-model closed-book vs RAG comparison; hallucination rate down ~58–60 points.
- Caveat from same paper: "retrieving 25 documents for RAG Token yields higher F1 scores, and lower perplexities, ... however, this also yields a lower Knowledge F1 score, and in human evaluations, we see higher levels of hallucination." [shuster2021] — more retrieved docs can raise hallucination (21.5% at 25 docs vs 17.0% at 5).

**Open-domain QA (EM): consistent single-digit to ~14-point gains.**
- "Table 9: GPT-3 16-shot prompting results ... GPT-3 Closed-Book 40.6 (NQ) 73.6 (TriviaQA) 20.2 (SQuAD); + Contriever top-10 44.2 (70.5%) 76.0 (75.1%) 34.0 (57.7%) ... Adding retrieval to GPT-3 consistently improves QA accuracy." [wang2023] — same model, RAG vs closed-book, exact match.
- PopQA/EntityQuestions: "Contriever gives 7% accuracy gains on top of GPT-3 davinci-003." and "retrieval augmentation can hurt the performance of large LMs on questions about popular entities as the retrieved context can be misleading." [mallen2023] — plus "For 10% of questions, retrieval-augmentation causes the LM to incorrectly answer a question it could otherwise answer correctly." [mallen2023]
- Rowen adaptive retrieval (2024): TruthfulQA GPT-Judge 59.34% vs SOTA baseline +16.74% (adaptive retrieval vs baselines, StrategyQA 75.60% accuracy) [rowen2024] — TruthfulQA gains from retrieval strategies; note baseline is other methods, not strictly closed-book.

**Long-form factuality / citations (ALCE).**
- "on the ELI5 dataset, around 50% generations of our ChatGPT and GPT-4 baselines are not fully supported by the cited passages" [gao2023] — residual hallucination even with retrieval.
- ASQA table: ChatGPT ClosedBook "52.7 | 38.3 | 26.7 | 26.7" vs Vanilla (5-psg) "66.6 | 40.4 | 73.6 | 72.5" (fluency/correctness/citation recall/precision). [gao2023] — retrieval's measured benefit is mostly citation/faithfulness (+47 pts recall), correctness only +2.1.
- Closed-book citation shortcut: "a closed-book model ... with post-hoc citing achieves good correctness but much worse citation quality" [gao2023].

**Medical / biomedical QA (named datasets).**
- MIRAGE/MEDRAG: "MEDRAG improves the accuracy of six different LLMs by up to 18% over chain-of-thought prompting" [xiong2024]. Table: GPT-4 CoT 73.44 → MEDRAG 79.97 avg; GPT-3.5 60.69 → 71.57; "PubMedQA* ... 36.00 → 67.40" (GPT-3.5) and "BioASQ-Y/N ... 74.27 → 90.29". [xiong2024]
- Negative/flat cases in same benchmark: "MRAG negatively impacts the MMNLU-Med task for most of the LLMs evaluated" and "relatively smaller LLMs ... can not utilize the retrieved documents to improve the performance on MedQA" [xiong2024-mrag] (2026 MRAG bench, arXiv 2601.16503); MIRAGE table shows GPT-4 MMLU-Med 89.44 → 87.24 with MEDRAG [xiong2024].
- BioASQ retrieval-strategy study (250 pairs, DeepEval): "All RAG conditions dramatically outperform the no-context ablation on answer relevancy (0.658–0.701 vs. 0.287)" but "No-context faithfulness is high by construction: DeepEval scores faithfulness against retrieved context" [bioasq-strategy2026] — faithfulness metrics can invert vs closed-book by construction.

**Retrieval can be net harmful (documented cases).**
- CRAG benchmark (Meta, 2024): "most advanced LLMs achieve ≤34% accuracy on CRAG, adding RAG in a straightforward manner improves the accuracy only to 44%. State-of-the-art industry RAG solutions only answer 63% of questions without any hallucination." and "none of the RAG solutions obtain truthfulness higher than 20%; this is because all RAG solutions introduce more hallucinations generated from irrelevant retrieval results" [cuconasu2024].
- RAGuard: "when exposed to misleading retrievals, all tested LLM-powered RAG systems perform worse than their zero-shot baselines (i.e., no retrieval at all)" [zhang2025-raguard].
- RAGGED: "FlanT5 achieves an average gain of 16-30 F1 points whereas GPT-3.5 achieves an average gain of 1 to 9 F1 points in comparison to closed-book generation" and "Models like LLaMa and Claude struggle with filtering noisy retrievals, which results in lower accuracy when using RAG." [ragged2024]
- Irrelevant-context study (5 ODQA benchmarks): "even with a strong retriever (top-1 Google search) incorporating the retrieved context actually hurts model performance on two of the benchmarks (StrategyQA and Fermi)" [roy2024-noise].

**Methodology caveats.**
- ALCE: "incorporating more retrieved passages in context does not help ChatGPT but improves GPT-4 performance" and "correctness plateaus at top-1 passage and citation quality plateaus at top-3" for ChatGPT [gao2023] — benchmark numbers depend on passage count and model.
- Faithfulness scoring: no-context conditions get faithfulness 0 by construction (contextual metrics), so closed-book vs RAG faithfulness comparisons need matching metrics [bioasq-strategy2026]; RAGTruth reports that even with RAG, 43.1% of responses contained hallucination spans overall (29.1% on QA) [ragtruth2024].
- "Benchmarking LLM Faithfulness in RAG" (Vectara, 2025) notes HHEM-style hallucination leaderboards track hallucination rates "since 2023" and that "large language models (LLMs) still frequently introduce unsupported information or contradictions even when provided with relevant context" [tamber2025].

## Conflicts
- Faithfulness metrics disagree under no-context baselines: one biomedical study reports no-context faithfulness 0.978 vs RAG ~0.90 because DeepEval scores against retrieved context [bioasq-strategy2026], while context-free factuality metrics (FActScore, SAFE) reward retrieval. Stronger sources (ALCE, MIRAGE, CRAG) use dataset-native metrics (EM, correctness, citation) — those show retrieval usually helping modestly.
- Magnitude of TruthfulQA gains: adaptive-retrieval papers claim +16.74 GPT-Judge points [rowen2024], but comparisons are often to other RAG baselines rather than a strictly closed-book same-model control; ITI (non-retrieval) reports Alpaca MC1 32.5 → 65.1 [iti2024] — non-retrieval interventions can match retrieval effects, so attribution to retrieval alone is confounded in some papers.
- Shuster et al. show more documents raises F1 while raising human-judged hallucination [shuster2021]; RAGGED shows reader-dependent gains [ragged2024]. These agree retrieval effect is configuration- and reader-dependent rather than a fixed constant.

## Gaps
- No single source gives an apples-to-apples RAG-vs-closed-book hallucination-rate table across all named sets (NQ, TriviaQA, HotpotQA, TruthfulQA, FEVER, HaluEval, ALCE); most papers cover 1–4 datasets with different metrics/models.
- HaluEval-specific RAG-vs-no-retrieval percentages were not located in a primary source (only indirect/co-occurrence reports, e.g., RAG+CoT lowest hallucination rate 11% on HaluEval in a 2025 preprint [cot-rag2025]).
- TruthfulQA retrieval-specific effect sizes vs pure closed-book same-model control were not found in a single primary quantitative table within the search limit.
- FEVER: only indirect evidence (e.g., ~90% accuracy with self-verification+RAG in a 2025 preprint [cot-rag2025]); no clean closed-book comparison located.
- HotpotQA: gains measured in filtering studies (FILCO +1.0/+1.3 F1) rather than RAG-vs-closed-book headline numbers [filco2023].

## sources
```sources
{"key": "shuster2021", "title": "Retrieval Augmentation Reduces Hallucination in Conversation", "authors": ["Kurt Shuster", "Spencer Poff", "Moya Chen", "Douwe Kiela", "Jason Weston"], "year": 2021, "doi": "10.18653/v1/2021.findings-emnlp.320", "url": "https://arxiv.org/abs/2104.07567", "quality": "A"}
{"key": "wang2023", "title": "Prompting GPT-3 To Be Reliable", "authors": ["Shuohang Wang", "Siyi Liu", "Xiangyang Mao", "Chongyang Tao", "Yiming Dong", "Jing Jiang"], "year": 2023, "url": "https://arxiv.org/html/2210.09150v2", "quality": "A"}
{"key": "mallen2023", "title": "When Not to Trust Language Models: Investigating Effectiveness of Parametric and Non-Parametric Memories (ACL 2023)", "authors": ["Alex Mallen", "Akari Asai", "Victor Zhong", "Rajarshi Das", "Daniel Khashabi", "Hannaneh Hajishirzi"], "year": 2023, "url": "https://arxiv.org/abs/2212.10511", "quality": "A"}
{"key": "gao2023", "title": "Enabling Large Language Models to Generate Text with Citations (ALCE)", "authors": ["Tianyu Gao", "Howard Yushun Chen", "Yiming Cui", "Jimmy Lin"], "year": 2023, "doi": "10.18653/v1/2023.emnlp-main.398", "url": "https://arxiv.org/html/2305.14627", "quality": "A"}
{"key": "xiong2024", "title": "Benchmarking Retrieval-Augmented Generation for Medicine (MIRAGE / MEDRAG)", "authors": ["Guangzhi Xiong", "Qiao Jin", "Zhiyong Lu", "Aidong Zhang"], "year": 2024, "url": "https://arxiv.org/abs/2402.13178", "quality": "A"}
{"key": "xiong2024-mrag", "title": "MRAG: Benchmarking Retrieval-Augmented Generation for Bio-medicine", "authors": ["Guangzhi Xiong", "Qiao Jin", "Zhiyong Lu", "Aidong Zhang"], "year": 2026, "url": "https://arxiv.org/html/2601.16503", "quality": "A"}
{"key": "cuconasu2024", "title": "The CRAG: Comprehensive RAG Benchmark", "authors": ["Fei Wang", "Kazunari Sugiyama", "et al. (Meta)"], "year": 2024, "url": "https://arxiv.org/pdf/2406.04744", "quality": "A"}
{"key": "zhang2025-raguard", "title": "Worse than Zero-shot? A Fact-Checking Dataset for Evaluating the Robustness of RAG Against Misleading Retrievals (RAGuard)", "authors": ["Zhang et al."], "year": 2025, "url": "https://arxiv.org/html/2502.16101v1", "quality": "A"}
{"key": "ragged2024", "title": "RAGGED: Towards Informed Design of Scalable and Stable RAG Systems", "authors": ["Asai et al."], "year": 2024, "url": "https://arxiv.org/html/2403.09040", "quality": "A"}
{"key": "bioasq-strategy2026", "title": "Benchmarking Retrieval Strategies for Biomedical Retrieval-Augmented Generation: A Controlled Empirical Study", "authors": ["et al."], "year": 2026, "url": "https://arxiv.org/pdf/2605.02520", "quality": "A"}
{"key": "ragtruth2024", "title": "RAGTruth: A Hallucination Corpus for Developing Trustworthy Retrieval-Augmented Language Models", "authors": ["Cheng Niu", "Yazhong Zhang", "Jahner Kieter", "Xiaoyu Shen", "Yonghao Zhu", "Yang Liu", "Tianwen Zhang", "Zhaochen Yun", "Juntao Li", "Min Zhang"], "year": 2024, "url": "https://arxiv.org/html/2401.00396", "quality": "A"}
{"key": "tamber2025", "title": "Benchmarking LLM Faithfulness in RAG with Evolving Leaderboards (Vectara)", "authors": ["Manveer Singh Tamber et al."], "year": 2025, "url": "https://arxiv.org/abs/2505.04847", "quality": "A"}
{"key": "rowen2024", "title": "Retrieve Only When It Needs: Adaptive Retrieval Augmentation for Hallucination Mitigation in Large Language Models", "authors": ["Rowen authors"], "year": 2024, "url": "https://arxiv.org/html/2402.10612v2", "quality": "B"}
{"key": "cot-rag2025", "title": "Improving the Reliability of LLMs: Combining Chain-of-Thought Reasoning and Retrieval-Augmented Generation", "authors": ["et al."], "year": 2025, "url": "https://arxiv.org/html/2505.09031", "quality": "B"}
{"key": "iti2024", "title": "Inference-Time Intervention: Eliciting Truthful Answers from a Language Model", "authors": ["Oam Patel et al."], "year": 2024, "url": "https://arxiv.org/html/2306.03341", "quality": "A"}
{"key": "roy2024-noise", "title": "Making Retrieval-Augmented Language Models Robust to Irrelevant Context", "authors": ["Devendra Singh Sachan et al. / Roy et al."], "year": 2024, "url": "https://arxiv.org/html/2310.01558v2", "quality": "A"}
{"key": "filco2023", "title": "Learning to Filter Context for Retrieval-Augmented Generation (FILCO)", "authors": ["Zhiruo Wang", "Jun Araki", "Zhengbao Jiang", "Md Rizwan Parvez", "Graham Neubig"], "year": 2023, "url": "https://arxiv.org/abs/2311.08377", "quality": "A"}
```
