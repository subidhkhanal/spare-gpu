# SQ3: Retrieval-side limits on RAG's hallucination reduction

## In brief
- Naive RAG can *increase* hallucination: on CRAG, straightforward RAG (GPT-4 Turbo) raised accuracy 33.5%→43.6% but hallucination 13.5%→30.1% (LLM-only → Task 3); "none of the RAG solutions obtain truthfulness higher than 20%; this is because all RAG solutions introduce more hallucinations generated from irrelevant retrieval results" [crag]. Noisy retrieval is the cause, not the generator.
- Corpus coverage caps trust: CRAG web-search recall is 85% at 50 pages / ~69% at top-5, and SOTA industry RAG still hallucinates on 16–25% of questions [crag].
- Missing/contradictory context drives hallucination: under unanswerable retrieved sets, Llama2-70B hallucinated 95.79% (NQ) / 94.06% (TriviaQA) of the time [robust-ralms] (TACL).
- Contradictory/corrupted context has dose-response effects: fooled rate 0.4%→72.0% as corrupted passages go 0→3 [tamper] (2026, arXiv, small study); "Hallucination on Hallucination" — erroneous retrieval compounds hallucinations [drag].
- Correct context fixes most errors (94% accuracy), but models recite wrong context when parametric prior is weak [clash-eval]; relevance labels correlate only *weakly* with downstream RAG performance (eRAG, SIGIR 2024) [erag], so retrieval metrics are an imperfect proxy for hallucination.
- Temporal staleness: CRAG shows "much lower accuracy in answering questions about facts with higher dynamism" [crag].

## Findings

**F1. Noisy retrieval increases hallucination (headline number).**
Quote: "none of the RAG solutions obtain truthfulness higher than 20%; this is because all RAG solutions introduce more hallucinations generated from irrelevant retrieval results, showing a big challenge in RAG— How to judiciously use retrieval results without being distracted by retrieval noises?" [crag]
Also: GPT-4 Turbo hallucination 13.5% (LLM-only) → 28.2% (Task 1) → 30.1% (Task 3); truthfulness 20.0 → 7.7 → 13.4 (Table 5, human-eval'd straightforward solutions).
Why it matters: direct evidence that retrieval-side noise (not model size) is what turns RAG's gain into a truthfulness loss.

**F2. Corpus coverage / recall limits.**
Quote: "the estimated web search recall curve for all CRAG questions, with an overall recall of 85% when using all 50 pages... the recall for the top 5 pages is about 69%." [crag]
Why it matters: even a perfect generator cannot beat ~69–85% answer coverage; missing evidence forces either abstention or hallucination (CRAG "Missing" = 35.9% for GPT-4 Turbo Task 1).

**F3. Missing retrieved context → hallucination, not abstention.**
Quote: "in the NQ dataset, Llama2 hallucinated 95.79% of the time, and 94.06% in the TriviaQA dataset... the models are mostly hallucinating" when failing to flag unanswerable document sets [robust-ralms].
Why it matters: retrieval miss (no answer in context) is a *hallucination trigger*, quantified on NQ/TriviaQA (TACL-accepted).

**F4. Contradictory / conflicting retrieved passages.**
Quote: "We identify three common scenarios-unanswerable, adversarial, conflicting-where retrieved document sets can confuse RALM... Our findings reveal that RALMs often fail to identify the unanswerability or contradiction of a document set, which frequently leads to hallucinations." [robust-ralms]
Why it matters: contradicts the assumption that LLMs detect disagreements among passages; failure to arbitrate conflict = hallucination.

**F5. Contradictory context dose-response.**
Quote: "the fooled rate runs 0.4%, 35.7%, 54.0% and 72.0% for zero through three corrupted passages" [tamper].
Why it matters: monotone link between amount of bad retrieved context and being fooled (i.e., hallucinating); caveat: 4-bit Llama 3.1 8B, FEVER, n≈1.9k generations, 2026 preprint — small/quantized setup.

**F6. Hallucination-on-hallucination (bad retrieval compounds errors).**
Quote: "erroneous or biased retrieval can mislead generation, compounding hallucinations, a phenomenon we term Hallucination on Hallucination." [drag]
Why it matters: names the mechanism; ACL 2025 (peer-reviewed).

**F7. Correct retrieval fixes most errors; wrong retrieval propagates when prior is weak.**
Quote: "providing the correct retrieved information fixes most model mistakes (94% accuracy). However, when the reference document is perturbed with increasing levels of wrong values, the LLM is more likely to recite the incorrect, modified information when its internal prior is weaker" [clash-eval] (NeurIPS 2024).
Why it matters: both directions — good retrieval → large hallucination/accuracy gain; bad retrieval → model obeys it unless prior is strong.

**F8. Retrieval relevance metrics ≠ downstream hallucination (weak proxy).**
Quote: "evaluation of the retrieval model's performance based on query-document relevance labels shows a small correlation with the RAG system's downstream performance." [erag]
Why it matters: standard recall/precision on relevance labels poorly predicts RAG output quality — a measurement gap between retrieval quality and hallucination rates.

**F9. Robustness under KB degradation (contradiction injection > off-topic noise).**
Quote: "Factual corruption and contradiction cause 10–71pp drops from clean retrieval... Relevance poisoning is comparatively benign (≤5pp drop)... semantic-fidelity corruptions overturn 25–72% of clean-answerable answers, while signal-utility corruptions overturn fewer than 10%." [ragstress]
Why it matters: contradictory/false retrieved content hurts far more than mere off-topic noise; 52,500 evaluations (2026, arXiv preprint).

**F10. Staleness / temporal dynamism.**
Quote: "CRAG also reveals much lower accuracy in answering questions regarding facts with higher dynamism, lower popularity, or higher complexity" [crag].
Why it matters: stale corpus (time-sensitive facts) is a retrieval-side accuracy/truthfulness limit; torso/tail entity truthfulness drops (GPT-4 21% Head → 8% Tail per paper summary).

## Conflicts
- Whether off-topic noise or semantic corruption dominates: RAGStress finds relevance poisoning benign (≤5pp) vs factual/contradiction (10–71pp) [ragstress]; CRAG attributes its hallucination rise largely to "irrelevant retrieval results" [crag]. Stronger/more controlled: RAGStress (52.5k evals); CRAG is a benchmark observation with human-eval. Both can hold — off-topic noise harms less *per document*, but in practice web noise still dominates because it is abundant.
- ClashEval shows 94% fix-rate with correct context [clash-eval] vs CRAG's truthfulness ceiling ≤20% for straightforward RAG [crag]: not a true contradiction — ClashEval uses curated correct reference docs; CRAG uses realistic noisy web retrieval. The gap between them *is* the retrieval-quality effect.
- RAGTruth: RAG still hallucinates "even with correct and relevant retrieved content" [ragtruth] — retrieval quality alone doesn't eliminate hallucination (generator-side residue). Also ReDeEP (2410.11414) same point.

## Gaps
- No single study cleanly separates recall vs precision vs staleness effects on hallucination rate with a shared metric (most are benchmarks with confounded retrieval+generation).
- Direct causal evidence "improved retrieval → lower hallucination rate" is mostly indirect (accuracy/truthfulness gains); eRAG shows relevance labels weakly correlate, so no validated retrieval metric maps to hallucination.
- Corpus-coverage-only experiments (answer absent vs present, everything else fixed) exist (unanswerable sets [robust-ralms]) but no large-scale study varying corpus staleness independently.
- Sources under 2026 (tamper, ragstress) are preprints — treat numbers as provisional.

## sources
```sources
{"key": "crag", "title": "CRAG: Comprehensive RAG Benchmark", "authors": ["Hao Xin et al."], "year": 2024, "doi": "10.48550/arXiv.2406.04744", "quality": "A"}
{"key": "robust-ralms", "title": "Toward Robust RALMs: Revealing the Impact of Imperfect Retrieval on Retrieval-Augmented Language Models", "authors": ["Seong-Il Park", "Jay-Yoon Lee"], "year": 2024, "doi": "10.48550/arXiv.2410.15107", "quality": "A"}
{"key": "clash-eval", "title": "ClashEval: Quantifying the tug-of-war between an LLM's internal prior and external evidence", "authors": ["Kevin Wu", "Eric Wu", "James Zou"], "year": 2024, "doi": "10.52202/079017-1053", "quality": "A"}
{"key": "erag", "title": "Evaluating Retrieval Quality in Retrieval-Augmented Generation (eRAG)", "authors": ["Alireza Salemi", "Hamed Zamani"], "year": 2024, "doi": "10.1145/3626772.3657957", "quality": "A"}
{"key": "drag", "title": "Removal of Hallucination on Hallucination: Debate-Augmented RAG", "authors": ["Wentao Hu et al."], "year": 2025, "doi": "10.18653/v1/2025.acl-long.770", "quality": "A"}
{"key": "ragtruth", "title": "RAGTruth: A Hallucination Corpus for Developing Trustworthy Retrieval-Augmented Language Models", "authors": ["Cheng Niu et al."], "year": 2024, "doi": "10.48550/arXiv.2401.00396", "quality": "A"}
{"key": "tamper", "title": "In RAG We Trust? Measuring Robustness of RAG Under Post-Retrieval Context Tampering", "authors": ["(arXiv preprint)"], "year": 2026, "url": "https://arxiv.org/abs/2609.09243", "quality": "B"}
{"key": "ragstress", "title": "RAGStress: A Controlled Benchmark for Evaluating RAG Under Knowledge-Base Degradation", "authors": ["L. Huang et al."], "year": 2026, "url": "https://arxiv.org/html/2610.04691v1", "quality": "B"}
```
