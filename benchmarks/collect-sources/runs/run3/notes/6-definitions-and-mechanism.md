# How is hallucination defined and typologized in the LM/RAG literature, and why does grounding on retrieved evidence reduce hallucination?

## In brief
- The canonical *pre-LLM* typology is intrinsic vs extrinsic: intrinsic = output contradicts the source; extrinsic = output unverifiable from the source. Ji et al. give the standard definitions [ji2023]. Maynez et al. is the origin of the factuality/faithfulness distinction, defining faithfulness against the source and factuality against world knowledge [maynez2020].
- The canonical *LLM-era* typology (Huang et al., TOIS) replaces this with two primary types: **factuality hallucination** (conflict with verifiable real-world facts) vs **faithfulness hallucination** (divergence from user input / lack of self-consistency), the latter split into instruction, context, and logical inconsistency [huang2025].
- Ji et al. explicitly reconcile the two vocabularies: "faithfulness" = consistent with the provided source (antonym of hallucination); "factuality" = actual/based on fact, where the "fact" may be source *or* world knowledge depending on the author — this is the crux of the factuality-vs-faithfulness and open-vs-closed-domain divide [ji2023].
- Farquhar et al. (Nature) narrows "hallucination" to a mechanistic subset, **confabulations** — fluent claims that are wrong *and arbitrary* (seed-sensitive) — and argues "hallucination" wrongly lumps distinct mechanisms together [farquhar2024].
- Mechanism: Lewis et al. attribute RAG's gains to hybrid parametric + non-parametric (retrieved) memory — grounding in a readable Wikipedia index makes the model "hallucinate" less [lewis2020]. Mechanistically, this is carried by specific **retrieval heads** (attention heads that copy context into the output): faithfulness correlates with them, and masking them induces unfaithful generations [huang-rhio2025]. Huang et al. localize the remaining failure to over-confidence/knowledge-conflict where the parametric prior beats the retrieved context [huang2025].

## Findings

**Huang et al. — the standard LLM-era taxonomy (factuality vs faithfulness).**
> "We categorize hallucination into two primary types: factuality hallucination and faithfulness hallucination. Factuality hallucination emphasizes the discrepancy between generated content and verifiable real-world facts, typically manifesting as factual inconsistencies. Conversely, faithfulness hallucination captures the divergence of generated content from user input or the lack of self-consistency within the generated content. This category is further subdivided into instruction inconsistency...; context inconsistency...; and logical inconsistency..." [huang2025]
Why it matters: this is the two-axis definition most RAG-hallucination work uses; factuality is judged against the world, faithfulness against the input/context — which is exactly what retrieval supplies.

**Huang et al. — restates the older intrinsic/extrinsic split it supersedes.**
> "hallucinations [are] defined as generated content that is either nonsensical or unfaithful to the provided source content. These hallucinations are categorized into two types: intrinsic hallucination, where the generated output contradicts the source content, and extrinsic hallucination, where the generated output cannot be verified from the source." [huang2025]
Why it matters: shows factuality/faithfulness is presented as a *refinement* of intrinsic/extrinsic, not a rival — Huang explicitly builds "upon the foundational work by Ji et al."

**Ji et al. — the standard NLG taxonomy and the terminology clarification.**
> "Intrinsic Hallucinations: The generated output that contradicts the source content. ... Extrinsic Hallucinations: The generated output that cannot be verified from the source content (i.e., output that can neither be supported nor contradicted by the source)." [ji2023]
> "Faithfulness is defined as staying consistent and truthful to the provided source – an antonym to 'hallucination.' ... Factuality refers to the quality of being actual or based on fact. Depending on what serves as the 'fact', 'factuality' and 'faithfulness' may or may not be the same. [Maynez et al. (2020)] differentiate 'factuality' from 'faithfulness' by defining the 'fact' to be the world knowledge. In contrast, [Dong et al. (2020)] use the source input as the 'fact'... making 'factuality' indistinguishable from 'faithfulness'." [ji2023]
Why it matters: this is the explicit statement of the factuality-vs-faithfulness conflict and its open/closed-domain root (world knowledge = open domain; fixed source = closed domain). Also notes extrinsic hallucination "is not always erroneous because it could be from factually correct external information" [ji2023].

**Maynez et al. — origin of the faithfulness/factuality split.**
> "these models are highly prone to hallucinate content that is unfaithful to the input document... we show that textual entailment measures better correlate with faithfulness than standard metrics" [maynez2020]
Why it matters: first large-scale human study separating *faithful-to-source* from *factually-true-in-the-world*; it is the reference both Ji and Farquhar cite for the two terms.

**Farquhar et al. — a competing, mechanism-grounded typology: confabulation.**
> "Hallucinations are often defined as LLMs generating 'content that is nonsensical or unfaithful to the provided source content'... but they have come to include a vast array of failures of faithfulness and factuality. We focus on a subset of hallucinations which we call 'confabulations' for which LLMs fluently make claims that are both wrong and arbitrary—by which we mean that the answer is sensitive to irrelevant details such as random seed." [farquhar2024]
> "Our probabilistic approach... detects an important class of hallucinations: those that are caused by a lack of LLM knowledge." [farquhar2024]
Why it matters: disagrees with the surveys' lumping — "We believe that combining these distinct mechanisms in the broad category hallucination is unhelpful" [farquhar2024]. Ties hallucination to *semantic uncertainty*, the mechanism grounding exploits.

**Lewis et al. 2020 — the original claim that retrieval grounding reduces factual error.**
> "the fact that it is more strongly grounded in real factual knowledge (in this case Wikipedia) makes it 'hallucinate' less with generations that are more factual, and offers more control and interpretability." [lewis2020]
> "RAG models hallucinate less and generate factually correct text more often than BART." ... "Evaluators indicated that BART was more factual than RAG in only 7.1% of cases, while RAG was more factual in 42.7% of cases" [lewis2020]
Why it matters: primary causal-style evidence — same architecture, adding a non-parametric (retrieved) memory flips human factuality judgments; this is the origin of the RAG-reduces-hallucination claim.

**Mechanism — retrieval heads carry context into output; their absence yields unfaithful generations.**
> "we identify a salient correlation between LFQA faithfulness and retrieval heads, a set of attention heads responsible for retrieving contextual information. ... Rhio first augments unfaithful samples ... by selectively masking retrieval heads. Then, these samples are incorporated into joint training, enabling the model to distinguish unfaithful outputs from faithful ones" [huang-rhio2025]
Why it matters: this is the concrete circuit-level account — grounding reduces hallucination because specific heads copy retrieved evidence into the answer; ablating them *causes* unfaithful output.

**Why grounding can still fail — parametric prior beats context.**
> "Retrieval-augmented LLMs generate answers through the combined effect of parametric knowledge and contextual knowledge. ... Knowledge conflicts in RAG are situations where contextual knowledge contradicts LLMs' parametric knowledge. [Longpre et al. (2021)] ... demonstrated that generative QA reader models (e.g. T5) tend to trust parametric memory over contextual information." [huang2025]
> "LLMs may sometimes exhibit over-confidence... language models often exhibit a localized focus within their attention mechanisms, giving priority to nearby words and resulting in a notable deficit in context attention... This insufficient attention can directly contribute to faithfulness hallucinations" [huang2025]
Why it matters: gives the causal boundary condition — grounding helps only when the model attends to and defers to the retrieved context over its stored prior.

## Conflicts
- **Factuality vs faithfulness as separate axes:** Maynez/Ji treat them as separable (world-knowledge fact vs source-faithfulness) and note Dong et al. make them identical by using source as "fact" [ji2023]. Huang et al. encode the distinction as its top-level two types [huang2025]. Stronger sources (Ji CSUR, Huang TOIS) agree the *pair* of axes is needed; they disagree only on whether the labels map to open- vs closed-domain rather than to two error kinds.
- **Scope of "hallucination":** the surveys define it broadly (any nonsensical/unfaithful output) [ji2023][huang2025]; Farquhar et al. argue the umbrella is *unhelpful* and restrict it to arbitrary, uncertainty-driven confabulations [farquhar2024]. The Nature paper is the dissenting, mechanism-first view; the surveys are the consensus-descriptive view.
- **Intrinsic/extrinsic vs factuality/faithfulness:** not a flat disagreement — Huang presents factuality/faithfulness as a re-evaluation for open-ended LLMs that "necessitates a reevaluation and adjustment of the existing taxonomy" [huang2025], while Ji keeps intrinsic/extrinsic as the standard NLG taxonomy [ji2023]. Both are "correct" for their scope (task-specific NLG vs general-purpose LLM).

## Gaps
- No direct fetch of Maynez et al.'s full body definitions (only abstract + how Ji/Farquhar/Huang cite it); the factuality="world knowledge" reading is sourced via Ji's paraphrase of Maynez, not a Maynez quote.
- "CiteFix" and "encoder injection" were not located in an allowed source; no primary paper found under those names within budget.
- No single paper found that quantifies the *uncertainty-reduction* mechanism (grounding → lower semantic entropy) end-to-end; Farquhar links hallucination to high semantic entropy but does not test RAG as the intervention.
- Open-domain vs closed-domain is treated as a *task* distinction (Lewis: open-domain vs closed-book QA) rather than an explicit hallucination typology label in the sources fetched.

## sources
{"key": "huang2025", "title": "A Survey on Hallucination in Large Language Models: Principles, Taxonomy, Challenges, and Open Questions", "authors": ["Lei Huang", "Weijiang Yu", "Weitao Ma", "Weihong Zhong", "Zhangyin Feng", "Haotian Wang", "Qianglong Chen", "Weihua Peng", "Xiaocheng Feng", "Bing Qin", "Ting Liu"], "year": 2024, "doi": "10.1145/3703155", "arxiv": "2311.05232", "quality": "A"}
{"key": "ji2023", "title": "Survey of Hallucination in Natural Language Generation", "authors": ["Ziwei Ji", "Nayeon Lee", "Rita Frieske", "Tiezheng Yu", "Dan Su", "Yan Xu", "Etsuko Ishii", "Yejin Bang", "Delong Chen", "Wenliang Dai", "Ho Shu Chan", "Andrea Madotto", "Pascale Fung"], "year": 2023, "doi": "10.1145/3571730", "arxiv": "2202.03629", "quality": "A"}
{"key": "maynez2020", "title": "On Faithfulness and Factuality in Abstractive Summarization", "authors": ["Joshua Maynez", "Shashi Narayan", "Bernd Bohnet", "Ryan McDonald"], "year": 2020, "doi": "10.18653/v1/2020.acl-main.173", "arxiv": "2005.00661", "quality": "A"}
{"key": "farquhar2024", "title": "Detecting hallucinations in large language models using semantic entropy", "authors": ["Sebastian Farquhar", "Jannik Kossen", "Lorenz Kuhn", "Yarin Gal"], "year": 2024, "doi": "10.1038/s41586-024-07421-0", "url": "https://www.nature.com/articles/s41586-024-07421-0", "date": "2024-06-19", "quality": "A"}
{"key": "lewis2020", "title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks", "authors": ["Patrick Lewis", "Ethan Perez", "Aleksandra Piktus", "Fabio Petroni", "Vladimir Karpukhin", "Naman Goyal", "Heinrich Küttler", "Mike Lewis", "Wen-tau Yih", "Tim Rocktäschel", "Sebastian Riedel", "Douwe Kiela"], "year": 2020, "arxiv": "2005.11401", "quality": "A"}
{"key": "huang-rhio2025", "title": "Improving Contextual Faithfulness of Large Language Models via Retrieval Heads-Induced Optimization", "authors": ["Lei Huang", "Xiaocheng Feng", "Weitao Ma", "Yuchun Fan", "Xiachong Feng", "Yangfan Ye", "Weihong Zhong", "Yuxuan Gu", "Baoxin Wang", "Dayong Wu", "Guoping Hu", "Bing Qin"], "year": 2025, "arxiv": "2501.13573", "quality": "A"}
