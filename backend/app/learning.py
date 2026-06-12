"""Phase 3: Continuous Learning System.

Tracks query patterns, usage statistics, and user feedback to improve
analysis and retrieval over time.
"""
from __future__ import annotations
from typing import List, Dict, Optional
from datetime import datetime
from collections import defaultdict
import re

from .models import (
    QueryPattern, UsageStatistics, TerminologyMapping,
    LearningInsights, GraphPayload
)
from . import storage


def _normalize_query(query: str) -> str:
    """Normalize query for pattern matching."""
    # Lowercase and remove punctuation
    normalized = query.lower()
    normalized = re.sub(r'[^\w\s]', ' ', normalized)
    # Remove extra whitespace
    normalized = ' '.join(normalized.split())
    return normalized


def _extract_terms(query: str) -> List[str]:
    """Extract meaningful terms from query."""
    normalized = _normalize_query(query)
    # Filter out common words
    stop_words = {
        'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
        'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'were', 'be',
        'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
        'would', 'should', 'could', 'may', 'might', 'must', 'can', 'this',
        'that', 'these', 'those', 'i', 'you', 'he', 'she', 'it', 'we', 'they',
        'what', 'which', 'who', 'when', 'where', 'why', 'how'
    }
    terms = [t for t in normalized.split() if len(t) > 2 and t not in stop_words]
    return terms


def record_query(
    graph_id: str,
    query: str,
    retrieved_nodes: List[str],
    response_time_ms: Optional[int] = None
) -> str:
    """Record a query for learning.
    
    Returns query_id for later feedback.
    """
    # Load or create learning insights
    insights = load_learning_insights(graph_id)
    
    # Create query pattern
    query_pattern = QueryPattern(
        query_text=query,
        graph_id=graph_id,
        retrieved_nodes=retrieved_nodes,
        response_time_ms=response_time_ms,
    )
    
    # Add to insights
    insights.query_patterns.append(query_pattern)
    insights.total_queries += 1
    
    # Update usage statistics for retrieved nodes
    for node_id in retrieved_nodes:
        if node_id not in insights.usage_stats:
            insights.usage_stats[node_id] = UsageStatistics(
                node_id=node_id,
                first_accessed=datetime.utcnow().isoformat()
            )
        
        stats = insights.usage_stats[node_id]
        stats.access_count += 1
        stats.last_accessed = datetime.utcnow().isoformat()
        
        # Keep only last 10 query contexts
        if query not in stats.query_contexts:
            stats.query_contexts.append(query)
            if len(stats.query_contexts) > 10:
                stats.query_contexts.pop(0)
    
    # Extract and update terminology
    terms = _extract_terms(query)
    if terms:
        # Load payload once and build a node-id → name index for O(1) lookups.
        payload = storage.load_light(graph_id)
        name_by_id: Dict[str, str] = {}
        if payload:
            name_by_id = {n.id: n.name for n in payload.function_nodes}
        retrieved_names = [name_by_id[nid] for nid in retrieved_nodes[:5] if nid in name_by_id]

        for term in terms:
            if term not in insights.terminology_map:
                insights.terminology_map[term] = TerminologyMapping(term=term)

            term_map = insights.terminology_map[term]
            term_map.frequency += 1

            for fname in retrieved_names:
                if fname not in term_map.mapped_functions:
                    term_map.mapped_functions.append(fname)

            term_map.confidence = min(1.0, term_map.frequency / 10.0)
    
    # Update timestamp
    insights.updated_at = datetime.utcnow().isoformat()
    
    # Save insights
    save_learning_insights(insights)
    
    return query_pattern.query_id


def record_feedback(
    graph_id: str,
    query_id: str,
    rating: str,
    comment: Optional[str] = None,
    corrections: Optional[Dict] = None
) -> bool:
    """Record user feedback on query results."""
    insights = load_learning_insights(graph_id)
    
    # Find the query pattern
    query_pattern = None
    for qp in insights.query_patterns:
        if qp.query_id == query_id:
            query_pattern = qp
            break
    
    if not query_pattern:
        return False
    
    # Update rating
    query_pattern.user_rating = rating
    
    if rating == "helpful":
        insights.helpful_queries += 1
        
        # Boost relevance scores for retrieved nodes
        for node_id in query_pattern.retrieved_nodes:
            if node_id in insights.usage_stats:
                stats = insights.usage_stats[node_id]
                # Increase average relevance
                stats.avg_relevance_score = min(1.0, stats.avg_relevance_score + 0.1)
    
    elif rating == "unhelpful":
        # Decrease relevance scores
        for node_id in query_pattern.retrieved_nodes:
            if node_id in insights.usage_stats:
                stats = insights.usage_stats[node_id]
                stats.avg_relevance_score = max(0.0, stats.avg_relevance_score - 0.1)
    
    # Apply corrections if provided
    if corrections:
        # User suggested better nodes
        if "better_nodes" in corrections:
            for node_id in corrections["better_nodes"]:
                if node_id not in insights.usage_stats:
                    insights.usage_stats[node_id] = UsageStatistics(
                        node_id=node_id,
                        first_accessed=datetime.utcnow().isoformat()
                    )
                stats = insights.usage_stats[node_id]
                stats.avg_relevance_score = min(1.0, stats.avg_relevance_score + 0.2)
    
    insights.updated_at = datetime.utcnow().isoformat()
    save_learning_insights(insights)
    
    return True


def get_hot_nodes(graph_id: str, limit: int = 10) -> List[Dict]:
    """Get most frequently accessed nodes."""
    insights = load_learning_insights(graph_id)
    
    # Sort by access count
    sorted_stats = sorted(
        insights.usage_stats.values(),
        key=lambda s: s.access_count,
        reverse=True
    )
    
    return [
        {
            "node_id": s.node_id,
            "access_count": s.access_count,
            "avg_relevance": s.avg_relevance_score,
            "last_accessed": s.last_accessed,
        }
        for s in sorted_stats[:limit]
    ]


def get_common_paths(graph_id: str, min_frequency: int = 3) -> List[List[str]]:
    """Identify frequently traversed execution paths."""
    insights = load_learning_insights(graph_id)
    
    # Group queries by retrieved node sequences
    path_frequency: Dict[tuple, int] = defaultdict(int)
    
    for qp in insights.query_patterns:
        if len(qp.retrieved_nodes) >= 2:
            # Use first 5 nodes as path signature
            path = tuple(qp.retrieved_nodes[:5])
            path_frequency[path] += 1
    
    # Filter by minimum frequency
    common_paths = [
        list(path) for path, freq in path_frequency.items()
        if freq >= min_frequency
    ]
    
    # Sort by frequency
    common_paths.sort(key=lambda p: path_frequency[tuple(p)], reverse=True)
    
    # Update insights
    insights.common_paths = common_paths[:20]  # Keep top 20
    save_learning_insights(insights)
    
    return common_paths


def get_terminology_suggestions(graph_id: str, query: str) -> List[str]:
    """Get function name suggestions based on learned terminology."""
    insights = load_learning_insights(graph_id)
    
    terms = _extract_terms(query)
    suggestions = []
    
    for term in terms:
        if term in insights.terminology_map:
            term_map = insights.terminology_map[term]
            # Add functions with high confidence
            if term_map.confidence > 0.1:
                suggestions.extend(term_map.mapped_functions)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_suggestions = []
    for func in suggestions:
        if func not in seen:
            seen.add(func)
            unique_suggestions.append(func)
    
    return unique_suggestions[:10]


def get_analytics(graph_id: str) -> Dict:
    """Get learning analytics for a graph."""
    insights = load_learning_insights(graph_id)
    
    # Calculate statistics
    total_queries = insights.total_queries
    helpful_rate = (insights.helpful_queries / total_queries * 100) if total_queries > 0 else 0
    
    # Most queried terms
    top_terms = sorted(
        insights.terminology_map.values(),
        key=lambda t: t.frequency,
        reverse=True
    )[:10]
    
    # Average response time
    response_times = [qp.response_time_ms for qp in insights.query_patterns if qp.response_time_ms]
    avg_response_time = sum(response_times) / len(response_times) if response_times else 0
    
    return {
        "total_queries": total_queries,
        "helpful_queries": insights.helpful_queries,
        "helpful_rate": round(helpful_rate, 2),
        "unique_nodes_accessed": len(insights.usage_stats),
        "common_paths_count": len(insights.common_paths),
        "learned_terms": len(insights.terminology_map),
        "avg_response_time_ms": round(avg_response_time, 2),
        "top_terms": [
            {"term": t.term, "frequency": t.frequency, "confidence": t.confidence}
            for t in top_terms
        ],
        "last_updated": insights.updated_at,
    }


def load_learning_insights(graph_id: str) -> LearningInsights:
    """Load learning insights from storage."""
    data = storage.load_meta(graph_id, "learning")
    if data:
        return LearningInsights(**data)
    return LearningInsights(graph_id=graph_id)


def save_learning_insights(insights: LearningInsights) -> None:
    """Save learning insights to storage."""
    storage.store_meta(insights.graph_id, "learning", insights.model_dump())


def update_node_importance(graph_id: str) -> None:
    """Update node criticality based on usage patterns.
    
    This can be called periodically to adjust semantic metadata
    based on actual usage.
    """
    insights = load_learning_insights(graph_id)
    payload = storage.load(graph_id)
    
    if not payload:
        return
    
    # Update criticality based on access patterns
    for node in payload.function_nodes:
        if node.id in insights.usage_stats:
            stats = insights.usage_stats[node.id]
            
            # High access count + high relevance = increase criticality
            if stats.access_count > 10 and stats.avg_relevance_score > 0.7:
                if node.semantics and node.semantics.criticality != "critical":
                    # Boost criticality
                    if node.semantics.criticality == "low":
                        node.semantics.criticality = "medium"
                    elif node.semantics.criticality == "medium":
                        node.semantics.criticality = "high"
                    elif node.semantics.criticality == "high":
                        node.semantics.criticality = "critical"
    
    # Save updated payload
    storage.save(payload)


def seed_terminology_from_graph(graph_id: str) -> None:
    """Pre-populate the terminology map from function names at ingest time.

    Without seeding, the map starts empty and needs 1+ real queries before
    the confidence threshold (0.1) is crossed.  This function pre-seeds every
    discriminative term found in function names so the very first query already
    benefits from the map.

    Skipped when real query data already exists (avoids overwriting learned data
    if the graph is re-ingested after use).
    """
    payload = storage.load_light(graph_id)
    if not payload or not payload.function_nodes:
        return

    insights = load_learning_insights(graph_id)
    if insights.total_queries > 0:
        return  # Real data exists — don't overwrite

    N = len(payload.function_nodes)
    max_df = max(1, int(N * 0.20))   # filter terms appearing in >20% of functions

    # Count how many functions each term appears in (document frequency)
    from collections import defaultdict as _dd
    term_df: dict = _dd(int)
    for node in payload.function_nodes:
        name_spaced = node.name.replace("_", " ").replace("-", " ")
        for term in set(_extract_terms(name_spaced)):
            term_df[term] += 1

    # Seed discriminative terms → function mappings
    for node in payload.function_nodes:
        name_spaced = node.name.replace("_", " ").replace("-", " ")
        for term in set(_extract_terms(name_spaced)):
            if term_df[term] > max_df:
                continue  # Too common to be useful (e.g., "get", "set", "init")

            if term not in insights.terminology_map:
                insights.terminology_map[term] = TerminologyMapping(term=term)

            term_map = insights.terminology_map[term]
            if node.name not in term_map.mapped_functions:
                term_map.mapped_functions.append(node.name)

            # frequency=4 → confidence=0.4 (above the 0.1 threshold after seeding)
            term_map.frequency = max(term_map.frequency, 4)
            term_map.confidence = min(1.0, term_map.frequency / 10.0)

    insights.updated_at = datetime.utcnow().isoformat()
    save_learning_insights(insights)


# Made with Bob
