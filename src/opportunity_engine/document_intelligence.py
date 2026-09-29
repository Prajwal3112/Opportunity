"""Retrieval-first document enrichment; no download or AI call happens implicitly."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field, model_validator

from wb_connector.models import Document

from .domain.models import CandidateInput, CapabilityProfile
from .matching.normalization import normalize_for_matching


class ProcessingStatus(StrEnum): PENDING="PENDING"; DOWNLOADED="DOWNLOADED"; EXTRACTED="EXTRACTED"; FAILED="FAILED"
class EvidenceAssessment(StrEnum): MATCH="MATCH"; PARTIAL_MATCH="PARTIAL_MATCH"; POTENTIAL_GAP="POTENTIAL_GAP"; NO_EVIDENCE="NO_EVIDENCE"; CONTRADICTED="CONTRADICTED"
class DocumentSelectionConfig(BaseModel): type_weights:dict[str,float]=Field(default_factory=dict); limit:int=5
class Chunk(BaseModel): chunk_id:str; document_id:str; project_id:str|None; sequence:int; source_url:str; text:str; page:int|None=None; heading:str|None=None; extraction_version:str="v1"; created_at:datetime
class RetrievedChunk(BaseModel): chunk:Chunk; lexical_score:float; semantic_score:float|None=None; combined_score:float
class EvidenceReference(BaseModel): document_id:str; chunk_id:str; page:int|None=None; source_url:str; quote:str
class CapabilityFinding(BaseModel): capability:str; assessment:EvidenceAssessment; evidence:list[EvidenceReference]=Field(default_factory=list)
class EvidenceAnalysis(BaseModel):
    candidate_id:str; relevance_tier:str; summary:str; capability_findings:list[CapabilityFinding]; evidence_sufficient:bool; analysis_version:str; model_name:str; prompt_version:str; analysed_at:datetime; retrieved_chunk_ids:list[str]; explicit_requirements:list[str]=Field(default_factory=list); technical_requirements:list[str]=Field(default_factory=list); service_requirements:list[str]=Field(default_factory=list); eligibility_requirements:list[str]=Field(default_factory=list); deliverables:list[str]=Field(default_factory=list); timeline_information:list[str]=Field(default_factory=list); geographic_requirements:list[str]=Field(default_factory=list); commercial_information:list[str]=Field(default_factory=list); contradictory_evidence:list[EvidenceReference]=Field(default_factory=list)
    @model_validator(mode="after")
    def guarded(self):
        if any(f.assessment!=EvidenceAssessment.NO_EVIDENCE and not f.evidence for f in self.capability_findings): raise ValueError("claims require evidence")
        return self
class EmbeddingProvider(Protocol):
    def embed(self,texts:list[str])->list[list[float]]: ...
class EvidenceAnalyzer(Protocol):
    def analyze(self,candidate:CandidateInput,profile:CapabilityProfile,passages:list[RetrievedChunk])->EvidenceAnalysis: ...
def select_documents(ds:list[Document],cfg:DocumentSelectionConfig)->list[Document]:
    return sorted(ds,key=lambda d:max((w for k,w in cfg.type_weights.items() if k in normalize_for_matching(d.document_type or "")),default=0),reverse=True)[:cfg.limit]
def checksum(b:bytes)->str:return hashlib.sha256(b).hexdigest()
def extract_text(b:bytes,typ:str)->str:
    if typ.startswith("text/") and (s:=b.decode("utf-8",errors="replace").strip()): return s
    raise RuntimeError("extraction failed or produced no text")
def chunk_text(doc:str,project:str|None,url:str,text:str,size:int=900)->list[Chunk]: return [Chunk(chunk_id=f"{doc}:{i}",document_id=doc,project_id=project,sequence=i,source_url=url,text=text[i*size:(i+1)*size],created_at=datetime.now(UTC)) for i in range((len(text)+size-1)//size)]
def retrieval_query(c:CandidateInput,p:CapabilityProfile)->str:return " ".join(filter(None,[c.bid_description,c.notice_text,*p.capabilities,*p.products,*p.services,*p.technologies,*p.positive_keywords]))
def lexical_retrieve(q:str,chunks:list[Chunk],limit:int=10)->list[RetrievedChunk]:
    terms=set(normalize_for_matching(q).split()); out=[RetrievedChunk(chunk=c,lexical_score=sum(normalize_for_matching(c.text).split().count(t) for t in terms),combined_score=0) for c in chunks]
    for r in out:r.combined_score=r.lexical_score
    return sorted(out,key=lambda r:r.lexical_score,reverse=True)[:limit]
MINIMUM_QUOTE_CHARACTERS = 30


def _collapse(text: str) -> str:
    """Compare on collapsed whitespace: PDF-extracted text rewraps unpredictably."""
    return " ".join(text.split())


def validate_analysis(analysis: EvidenceAnalysis, chunks: list[Chunk]) -> None:
    """Reject an analysis whose citations are not grounded in the retrieved chunks.

    This is provenance validation, not truth validation. It establishes that a quote
    really came from a retrieved passage; it cannot establish that the passage is
    honest, because a bidder can author the source document. Prompt-level fencing of
    untrusted passages is a separate, still-required control.
    """
    known = {chunk.chunk_id: _collapse(chunk.text) for chunk in chunks}

    def check(reference: EvidenceReference, where: str) -> None:
        if reference.chunk_id not in known:
            raise ValueError(f"{where} cites unknown chunk {reference.chunk_id!r}")
        quote = _collapse(reference.quote)
        # An empty quote is a substring of every string, so without a floor the
        # guardrail accepts a fabricated claim carrying `quote=""`.
        if len(quote) < MINIMUM_QUOTE_CHARACTERS:
            raise ValueError(
                f"{where} quote is {len(quote)} characters; at least "
                f"{MINIMUM_QUOTE_CHARACTERS} are required to be verifiable"
            )
        if quote not in known[reference.chunk_id]:
            raise ValueError(f"{where} quote does not appear in chunk {reference.chunk_id!r}")

    for finding in analysis.capability_findings:
        for reference in finding.evidence:
            check(reference, f"capability {finding.capability!r}")
    for reference in analysis.contradictory_evidence:
        check(reference, "contradictory_evidence")
    for chunk_id in analysis.retrieved_chunk_ids:
        if chunk_id not in known:
            raise ValueError(f"retrieved_chunk_ids names unknown chunk {chunk_id!r}")
