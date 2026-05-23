from typing import List, Optional, Literal, Dict, Any, Union
from pydantic import BaseModel, Field
from datetime import datetime
import uuid


NodeType = Literal["function", "class", "method", "file"]
EdgeType = Literal["CALLS", "DEFINES", "IMPORTS", "INHERITS", "OVERRIDES", "FLOW"]

# Canonical Intermediate Representation (CIR)
#
# The CIR is the language-agnostic graph contract used by the pipeline core.
# Language adapters may still expose legacy Python-friendly `type` values for
# existing API/UI compatibility, but core behavior should prefer `kind` and
# `relationship`.
CIR_VERSION = "cir.v1"
CIRNodeKind = Literal["file", "namespace", "type", "function", "callable", "data", "external"]
CIRRelationship = Literal[
    "CONTAINS",
    "INVOKES",
    "DEPENDS_ON",
    "EXTENDS",
    "IMPLEMENTS",
    "OVERRIDES",
    "FLOWS_TO",
]
CIRTypeSystem = Literal["explicit", "inferred", "dynamic", "unknown"]


class CIRSourceSpan(BaseModel):
    """Language-neutral source position for a node."""

    start_line: Optional[int] = None
    end_line: Optional[int] = None
    start_column: Optional[int] = None
    end_column: Optional[int] = None


class CIRSignature(BaseModel):
    """Language-neutral callable/type signature summary."""

    parameters: List[Dict[str, Any]] = Field(default_factory=list)
    returns: Optional[str] = None
    type_system: CIRTypeSystem = "unknown"

# Phase 2: Semantic Edge Types
SemanticEdgeType = Literal[
    "TRANSFORMS",      # Data transformation
    "VALIDATES",       # Input/state validation
    "ORCHESTRATES",    # Coordinates operations
    "PERSISTS",        # Saves data
    "RETRIEVES",       # Loads data
    "CONFIGURES",      # System setup
    "HANDLES",         # Error handling
    "DEPENDS_ON",      # Logical dependency
    "PRODUCES",        # Creates/generates
    "CONSUMES"         # Uses/processes
]


# Phase 1: Repository Intelligence Layer Models

class RepositoryContext(BaseModel):
    """Metadata from Bob's repository-level analysis."""
    
    project_type: Literal[
        "web_api", "cli_tool", "library", "desktop_app",
        "data_pipeline", "ml_model", "microservices", "unknown"
    ] = "unknown"
    domain: str = Field(
        default="general",
        description="Business/technical domain (e.g., 'code_analysis', 'e-commerce')"
    )
    architecture: Literal[
        "monolith", "modular_pipeline", "microservices",
        "mvc", "layered", "event_driven", "plugin", "unknown"
    ] = "unknown"
    tech_stack: List[str] = Field(
        default_factory=list,
        description="Key technologies/frameworks detected"
    )
    purpose: str = Field(
        default="",
        description="High-level description of what the project does"
    )
    key_entry_points: List[str] = Field(
        default_factory=list,
        description="Main files that serve as entry points"
    )
    critical_modules: List[str] = Field(
        default_factory=list,
        description="Core modules identified by Bob"
    )
    data_flow_pattern: Optional[str] = Field(
        None,
        description="High-level data flow description"
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0, le=1.0,
        description="Bob's confidence in this analysis"
    )
    analyzed_at: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat(),
        description="ISO timestamp of analysis"
    )
    fallback_used: bool = Field(
        default=False,
        description="True if heuristic fallback was used instead of Bob API"
    )


# Phase 2: Semantic Metadata Models

class SemanticMetadata(BaseModel):
    """Semantic analysis from Bob (Phase 2)."""
    
    intent: Literal[
        "orchestration", "transformation", "validation",
        "persistence", "retrieval", "configuration",
        "error_handling", "computation", "presentation", "unknown"
    ] = "unknown"
    complexity: Literal["low", "medium", "high", "very_high"] = "medium"
    criticality: Literal["low", "medium", "high", "critical"] = "medium"
    patterns: List[str] = Field(
        default_factory=list,
        description="Design patterns detected (e.g., 'factory', 'strategy')"
    )
    side_effects: List[Literal[
        "file_io", "network", "database", "state_mutation",
        "logging", "none"
    ]] = Field(default_factory=list)
    data_flow: Optional[Dict[str, Any]] = Field(
        None,
        description="Input/output/transformation description"
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0, le=1.0,
        description="Bob's confidence in this analysis"
    )


class SemanticEdge(BaseModel):
    """Semantic relationship between nodes."""
    
    type: SemanticEdgeType
    source_id: str
    target_id: str
    confidence: float = Field(
        default=1.0,
        ge=0.0, le=1.0,
        description="Confidence in this relationship"
    )
    description: Optional[str] = Field(
        None,
        description="Human-readable explanation of the relationship"
    )
    inferred_by: Literal["bob", "ast", "heuristic"] = "bob"


_LEGACY_NODE_TO_KIND: Dict[str, CIRNodeKind] = {
    "file": "file",
    "class": "type",
    "function": "function",
    "method": "function",
}

_LEGACY_EDGE_TO_RELATIONSHIP: Dict[str, CIRRelationship] = {
    "DEFINES": "CONTAINS",
    "CALLS": "INVOKES",
    "IMPORTS": "DEPENDS_ON",
    "INHERITS": "EXTENDS",
    "OVERRIDES": "OVERRIDES",
    "FLOW": "FLOWS_TO",
}


class CIRNode(BaseModel):
    """Canonical language-agnostic graph node.

    `type` is retained for current clients that render file/class/function/method
    directly. New pipeline code should prefer `kind`, `source_language`,
    `qualified_name`, `signature`, and `adapter_metadata`.
    """

    id: str
    name: str
    file_path: str
    type: NodeType
    kind: Optional[CIRNodeKind] = None
    cir_version: str = CIR_VERSION
    source_language: str = "unknown"
    qualified_name: Optional[str] = None
    source_span: Optional[CIRSourceSpan] = None
    signature: Optional[CIRSignature] = None
    adapter_metadata: Dict[str, Any] = Field(default_factory=dict)
    code_snippet: Optional[str] = None
    summary: Optional[str] = None
    lineno: Optional[int] = None
    
    # Phase 2: Semantic enrichment
    semantics: Optional[SemanticMetadata] = None

    def model_post_init(self, __context: Any) -> None:
        if self.kind is None:
            self.kind = _LEGACY_NODE_TO_KIND.get(self.type, "external")
        if self.lineno is None and self.source_span and self.source_span.start_line:
            self.lineno = self.source_span.start_line


class CIREdge(BaseModel):
    """Canonical language-agnostic graph edge.

    `type` is retained for current clients. New pipeline code should prefer
    `relationship`.
    """

    type: EdgeType
    relationship: Optional[CIRRelationship] = None
    source_id: str
    target_id: str
    cir_version: str = CIR_VERSION
    adapter_metadata: Dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        if self.relationship is None:
            self.relationship = _LEGACY_EDGE_TO_RELATIONSHIP.get(self.type, "DEPENDS_ON")


class FunctionNode(CIRNode):
    """Compatibility alias for existing function graph clients."""


class FunctionEdge(CIREdge):
    """Compatibility alias for existing function graph clients."""


class ModuleNode(BaseModel):
    id: str
    name: str
    description: str = ""
    depth_level: int = 1
    linked_function_ids: List[str] = Field(default_factory=list)
    is_entry_point: bool = Field(
        default=False,
        description="True if this module contains entry point functions"
    )
    entry_functions: List[str] = Field(
        default_factory=list,
        description="List of function IDs that are entry points within this module"
    )
    control_flow_groups: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Groups of functions by control flow pattern (e.g., 'validation_branch', 'error_handling')"
    )
    submodule_type: Optional[Literal["entry", "core", "utility", "control_flow"]] = Field(
        default=None,
        description="Classification of submodule purpose"
    )


class ModuleEdge(BaseModel):
    type: Literal["FLOW"] = "FLOW"
    source_module_id: str
    target_module_id: str


class GraphPayload(BaseModel):
    graph_id: str
    repo_path: str
    cir_version: str = CIR_VERSION
    function_nodes: List[FunctionNode]
    function_edges: List[FunctionEdge]
    module_nodes: List[ModuleNode]
    module_edges: List[ModuleEdge]
    module_to_functions: Dict[str, List[str]]
    
    # Phase 2: Semantic enrichment
    semantic_edges: List[SemanticEdge] = Field(
        default_factory=list,
        description="Semantic relationships between nodes"
    )


class LLMIngestionNode(BaseModel):
    """LLM-normalized node-level interpretation of AST ingestion results."""

    node_id: str
    name: str
    file_path: str
    node_type: NodeType
    module_id: Optional[str] = None
    module_name: Optional[str] = None
    role: str = ""
    responsibilities: List[str] = Field(default_factory=list)
    inputs: List[str] = Field(default_factory=list)
    outputs: List[str] = Field(default_factory=list)
    dependencies: List[str] = Field(default_factory=list)
    side_effects: List[str] = Field(default_factory=list)
    summary: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class LLMIngestionResult(BaseModel):
    """Validated JSON payload returned by the LLM ingestion stage."""

    graph_id: Optional[str] = None
    repo_path: Optional[str] = None
    prompt_version: str = "v1"
    generated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    nodes: List[LLMIngestionNode] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class IngestRequest(BaseModel):
    repo_path: str
    repo_id: Optional[str] = Field(
        None,
        description="Optional stable identifier for the repository. If not provided, generated from repo_path hash."
    )


class BobGraphRequest(BaseModel):
    """Request for Bob/MCP clients that need a generated graph in one call."""

    repo_path: str
    depth: int = Field(
        default=3,
        ge=1,
        le=3,
        description="Collapsed graph view depth to include for query answering."
    )
    include_full_graph: bool = Field(
        default=True,
        description="Include full GraphPayload with CIR nodes and edges."
    )
    include_view: bool = Field(
        default=True,
        description="Include a collapsed query-friendly graph view."
    )
    include_llm_ingestion: bool = Field(
        default=True,
        description="Include LLM-normalized ingestion metadata when available."
    )


class UpdateRequest(BaseModel):
    graph_id: str


class QueryRequest(BaseModel):
    graph_id: str
    query: str
    depth: int = Field(
        default=2,
        ge=1,
        le=5,
        description="Maximum hops for subgraph traversal"
    )


class QueryResponse(BaseModel):
    explanation: str
    subgraph: Dict[str, Any]
    path: List[str]
    query_id: Optional[str] = None


# MCP-specific normalized models
class MCPIngestResponse(BaseModel):
    """Normalized response for MCP ingest_repo tool."""
    
    success: bool
    graph_id: str
    repo_id: str = Field(description="Stable repository identifier")
    repo_path: str
    function_count: int
    module_count: int
    repo_context: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class MCPQueryResponse(BaseModel):
    """Normalized response for MCP query_repo tool."""
    
    success: bool
    graph_id: str
    query: str
    explanation: str
    relevant_functions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="List of relevant function nodes with metadata"
    )
    execution_path: List[str] = Field(
        default_factory=list,
        description="Ordered list of function IDs in execution flow"
    )
    query_id: Optional[str] = None
    error: Optional[str] = None


# Phase 3: Continuous Learning Models

class QueryPattern(BaseModel):
    """Track query patterns for learning."""
    
    query_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        description="Unique query identifier"
    )
    query_text: str
    normalized_query: str = Field(
        description="Lowercased, tokenized version for matching"
    )
    graph_id: str
    retrieved_nodes: List[str] = Field(
        default_factory=list,
        description="Node IDs retrieved for this query"
    )
    user_rating: Optional[Literal["helpful", "neutral", "unhelpful"]] = None
    timestamp: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )
    response_time_ms: Optional[int] = None


class UsageStatistics(BaseModel):
    """Node/edge usage tracking."""
    
    node_id: str
    access_count: int = 0
    query_contexts: List[str] = Field(
        default_factory=list,
        description="Queries that led to this node (max 10)"
    )
    avg_relevance_score: float = Field(
        default=0.0, ge=0.0, le=1.0
    )
    first_accessed: Optional[str] = None
    last_accessed: Optional[str] = None


class TerminologyMapping(BaseModel):
    """Domain-specific terminology learned from queries."""
    
    term: str
    mapped_functions: List[str] = Field(
        default_factory=list,
        description="Function names associated with this term"
    )
    frequency: int = 0
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class LearningInsights(BaseModel):
    """Accumulated learning for a graph."""
    
    graph_id: str
    query_patterns: List[QueryPattern] = Field(default_factory=list)
    usage_stats: Dict[str, UsageStatistics] = Field(
        default_factory=dict,
        description="Keyed by node_id"
    )
    common_paths: List[List[str]] = Field(
        default_factory=list,
        description="Frequently traversed execution paths"
    )
    terminology_map: Dict[str, TerminologyMapping] = Field(
        default_factory=dict,
        description="Domain terms → function mappings"
    )
    total_queries: int = 0
    helpful_queries: int = 0
    updated_at: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )


class FeedbackRequest(BaseModel):
    """User feedback on query results."""
    
    graph_id: str
    query_id: str
    rating: Union[Literal["helpful", "neutral", "unhelpful"], int, float]
    comment: Optional[str] = None
    corrections: Optional[Dict[str, Any]] = Field(
        None,
        description="User corrections to improve results"
    )
