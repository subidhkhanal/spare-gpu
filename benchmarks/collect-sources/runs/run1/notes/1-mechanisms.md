# What is the standard RAG architecture, and by what mechanisms is retrieval claimed to reduce hallucination?

## In brief

- Standard architecture (seminal): RAG = pre-trained seq2seq generator + non-parametric memory as a dense vector index accessed by a pre-trained neural retriever, trained end-to-end; the generator conditions on input plus retrieved passages [lewis2020].
- LLM-era generalization: retrieve → augment (condition on retrieved chunks) → generate; surveys formalize this as retrieval/augmentation/generation stages and Naive → Advanced → Modular RAG paradigms [gao2023survey] [wang2024survey].
- Mechanism 1, grounding in evidence: conditioning generation on retrieved documents is claimed to reduce hallucination and "the problem of generating factually incorrect content" [zhou2024trust] [gao2023survey] [ayala2024].
- Mechanism 2, offloading from parametric memory: knowledge lives in a swappable index that "can be directly revised and expanded," addressing parametric memory's inability to be updated; RA-LLMs use external knowledge "rather than solely relying on the model's internal knowledge" [lewis2020] [wang2024survey].
- Mechanism 3, attribution/citation: RAG was motivated by "providing provenance for their decisions"; trust surveys track transparency/attribution as first-class dimensions; Self-RAG adds explicit reflection on whether retrieved passages support the output [lewis2020] [zhou2024trust] [asai2023selfrag].
- Mechanism 4, fusion into generation: passages are fused by top-K marginalization over latent documents, per-output or per-token, in decoding [lewis2020].
- Caveat: the reduction claim is contested — retrieved but irrelevant content can increase "external hallucinations," and unreliable retrieval still yields undesirable outputs [rowen2025] [asai2023selfrag] [zhou2024trust].

## Findings

- [lewis2020] — Seminal 2020 paper defining the standard architecture.
  - Architecture/offload: "We introduce RAG models where the parametric memory is a pre-trained seq2seq model and the non-parametric memory is a dense vector index of Wikipedia, accessed with a pre-trained neural retriever."
  - Fusion: "The retriever (Dense Passage Retriever [26], henceforth DPR) provides latent documents conditioned on the input, and the seq2seq model (BART) then conditions on these latent documents together with the input to generate the output. We marginalize the latent documents with a top-K approximation, either on a per-output basis (assuming the same document is responsible for all tokens) or a per-token basis."
  - Why offload works: "Hybrid models that combine parametric memory with non-parametric (i.e., retrieval-based) memories ... can address some of these issues because knowledge can be directly revised and expanded, and accessed knowledge can be inspected and interpreted."
  - Updateability (offload): "Finally, we demonstrate that the non-parametric memory can be replaced to update the models' knowledge as the world changes."
  - Attribution motivation: "providing provenance for their decisions and updating their world knowledge remain open research problems."
  - Factual-quality claim: "RAG models generate more specific, diverse and factual language than a state-of-the-art parametric-only seq2seq baseline."
  - Why it matters: single source that defines the architecture and all four mechanism families (index offload, fusion, provenance, factual grounding) at once.
- [gao2023survey] — Major 2023/2024 survey (arXiv Dec 2023; 6,200+ citations).
  - "By referencing external knowledge, RAG effectively reduces the problem of generating factually incorrect content."
  - "This enhances the accuracy and credibility of the generation, particularly for knowledge-intensive tasks, and allows for continuous knowledge updates."
  - Architecture framing: "the tripartite foundation of RAG frameworks, which includes the retrieval, the generation and the augmentation techniques," with "Naive RAG, the Advanced RAG, and the Modular RAG" paradigms.
  - Why it matters: the most-cited canonical description of the pipeline and of the hallucination-reduction claim in the LLM era.
- [wang2024survey] — KDD 2024 survey of RAG meeting LLMs.
  - "Retrieval-Augmented Large Language Models (RA-LLMs) have emerged to harness external and authoritative knowledge bases, rather than solely relying on the model's internal knowledge, to augment the generation quality of LLMs."
  - "covering three primary technical perspectives: architectures, training strategies, and applications."
  - Why it matters: frames offloading from parametric memory as the defining purpose of RA-LLMs.
- [zhou2024trust] — Trustworthiness survey (arXiv 2409.10102; fetched revision dated 2026-05-16).
  - "RAG can improve LLM reliability by grounding responses in external and up-to-date knowledge, reducing hallucinations. However, unreliable retrieval or improper knowledge utilization may still lead to undesirable outputs."
  - "Trust-RAG Compass, that assesses the trustworthiness of RAG systems across six key dimensions: factuality, robustness, fairness, transparency, accountability, and privacy."
  - Why it matters: states the grounding mechanism and explicitly hedges it; transparency/accountability are where attribution/citation is organized.
- [asai2023selfrag] — Self-RAG (arXiv Oct 2023; ICLR 2024).
  - "large language models (LLMs) often produce responses containing factual inaccuracies due to their sole reliance on the parametric knowledge they encapsulate. Retrieval-Augmented Generation (RAG), an ad hoc approach that augments LMs with retrieval of relevant knowledge, decreases such issues."
  - "However, indiscriminately retrieving and incorporating a fixed number of retrieved passages, regardless of whether retrieval is necessary, or passages are relevant" (abstract truncated here) — motivates adaptive retrieval and per-sentence support critique.
  - Why it matters: A-rated follow-up architecture work that both endorses and refines the mechanism (relevance/support checking instead of fixed-k fusion).
- [rowen2025] — Rowen, SIGIR-AP 2025, hallucination mitigation.
  - "The utilization of parametric knowledge in generating factual content is constrained by the limited knowledge of LLMs, potentially resulting in internal hallucinations. While incorporating external information can help fill knowledge gaps, it also introduces the risk of irrelevant information, thereby increasing the likelihood of external hallucinations."
  - Why it matters: the clearest statement that retrieval reduces internal (parametric) hallucination but can create external (retrieval-induced) hallucination — the main qualification of the claim.
- [ayala2024] — ServiceNow, NAACL 2024 Industry Track.
  - "Retrieval-Augmented Generation (RAG) is a well-known method that can reduce hallucination and improve output quality, especially when generating the correct output requires access to external knowledge sources."
  - "Thanks to our implementation of RAG, our proposed system significantly reduces hallucination and allows the generalization of our LLM to out-of-domain settings."
  - Why it matters: peer-reviewed deployment evidence that grounding in retrieved (domain-specific) knowledge reduces hallucination in structured output, and that a good retriever can shrink the LLM.

## Conflicts

- The surveys' claim that RAG reduces hallucination [gao2023survey] [zhou2024trust] [ayala2024] is qualified by the mechanism literature itself: retrieved-but-irrelevant content increases "external hallucinations" [rowen2025]; fixed-k indiscriminate retrieval is called "ad hoc" [asai2023selfrag]; "unreliable retrieval or improper knowledge utilization may still lead to undesirable outputs" [zhou2024trust]. The stronger, more recent peer-reviewed sources (Rowen 2025, Self-RAG/ICLR 2024) take the qualified view; the blanket claim survives mainly in survey introductions.
- Subtle split within "hallucination": sources treat internal (parametric) vs external (context-induced) hallucinations differently — retrieval targets the former and can worsen the latter [rowen2025].

## Gaps

- Magnitude: I did not fetch full papers, so quantitative hallucination-reduction results (e.g., exact NQ/FEVER/faithfulness deltas for RAG vs parametric-only baselines in Lewis 2020) are not quoted.
- Citation-specific mechanism: a dedicated attributed-generation paper (e.g., ALCE, Gao et al. 2023 ACL) was not retrievable in searches with a quotable abstract; attribution is evidenced here via provenance motivation [lewis2020] and trust dimensions [zhou2024trust] only.
- 2025/2026 architecture follow-ups (GraphRAG, corrective/agentic RAG) exist (e.g., Peng et al. 2025, doi:10.1145/3777378) but were not fetched/quoted, so their mechanisms are out of scope here.
- Exact NeurIPS 2020 venue/DOI for the Lewis paper beyond the arXiv record was not confirmed from a fetched page.

```sources
{"key": "lewis2020", "title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks", "authors": ["Patrick Lewis", "Ethan Perez", "Aleksandra Piktus", "Fabio Petroni", "Vladimir Karpukhin", "Naman Goyal", "Heinrich Kuttler", "Mike Lewis", "Wen-tau Yih", "Tim Rocktaschel", "Sebastian Riedel", "Douwe Kiela"], "year": 2020, "arxiv": "2005.11401", "doi": "10.48550/arxiv.2005.11401", "openalex": "W3027879771", "url": "https://arxiv.org/abs/2005.11401", "quality": "A"}
{"key": "gao2023survey", "title": "Retrieval-Augmented Generation for Large Language Models: A Survey", "authors": ["Yunfan Gao", "Yun Xiong", "Xinyu Gao", "Kangxiang Jia", "Jinliu Pan", "Yuxi Bi", "Yi Dai", "Jiawei Sun", "Meng Wang", "Haofen Wang"], "year": 2023, "arxiv": "2312.10997", "url": "https://arxiv.org/abs/2312.10997", "quality": "B"}
{"key": "wang2024survey", "title": "A Survey on RAG Meeting LLMs: Towards Retrieval-Augmented Large Language Models", "authors": ["Wenqi Fan", "Yujuan Ding", "Liangbo Ning", "Shijie Wang", "Hengyun Li", "Dawei Yin", "Tat-Seng Chua", "Qing Li"], "year": 2024, "arxiv": "2405.06211", "doi": "10.1145/3637528.3671470", "url": "https://arxiv.org/abs/2405.06211", "quality": "B"}
{"key": "zhou2024trust", "title": "Trustworthiness in Retrieval-Augmented Generation Systems: A Survey", "authors": ["Yujia Zhou", "Wenbo Zhang", "Jingying Shao", "Yan Liu", "Xiaoxi Li", "Jiajie Jin", "Hongjin Qian", "Zheng Liu", "Chaozhuo Li", "Jason Chen Zhang", "Zhicheng Dou", "Philip S. Yu", "Jiaxin Mao"], "year": 2024, "arxiv": "2409.10102", "url": "https://arxiv.org/abs/2409.10102", "date": "fetched revision published 2026-05-16", "quality": "B"}
{"key": "asai2023selfrag", "title": "Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection", "authors": ["Akari Asai", "Zeqiu Wu", "Yizhong Wang", "Avirup Sil", "Hannaneh Hajishirzi"], "year": 2023, "arxiv": "2310.11511", "doi": "10.48550/arxiv.2310.11511", "url": "https://arxiv.org/abs/2310.11511", "quality": "A"}
{"key": "rowen2025", "title": "Rowen: Adaptive Retrieval-Augmented Generation for Hallucination Mitigation in LLMs", "authors": ["Hanxing Ding", "Liang Pang", "Zihao Wei", "Huawei Shen", "Xueqi Cheng"], "year": 2025, "doi": "10.1145/3767695.3769500", "url": "https://doi.org/10.1145/3767695.3769500", "quality": "A"}
{"key": "ayala2024", "title": "Reducing hallucination in structured outputs via Retrieval-Augmented Generation", "authors": ["Orlando Marquez Ayala", "Patrice Bechard"], "year": 2024, "arxiv": "2404.08189", "doi": "10.18653/v1/2024.naacl-industry.19", "url": "https://arxiv.org/abs/2404.08189", "quality": "A"}
```
