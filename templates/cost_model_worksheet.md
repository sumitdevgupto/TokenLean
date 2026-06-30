# Agent Decomposition Cost Modeling Worksheet

> Use this worksheet to decide between monolithic vs decomposed agent architectures.

## Quick Calculator

```python
from templates.langgraph_decomposition import DecompositionCostModel

# Run analysis
result = DecompositionCostModel.analyze_workflow_efficiency()
print(f"Recommended: {result['recommendation']}")
print(f"Savings: {result['savings_percent']:.1f}%")
```

---

## Decision Framework

### Step 1: Identify Intents
List the distinct user intents your agent handles:

| Intent | Frequency | Complexity | Latency Requirement |
|--------|-----------|------------|---------------------|
| ___    | ___%      | Low/Med/High | Fast/Med/Slow     |
| ___    | ___%      | Low/Med/High | Fast/Med/Slow     |
| ___    | ___%      | Low/Med/High | Fast/Med/Slow     |

**Rule of Thumb**: ≥3 distinct intents with different handling patterns → **Decompose**

---

### Step 2: Token Context Analysis

#### Monolithic Approach
```
System prompt tokens: ______
Avg context per request: ______
Model: ______
```

**Monolithic Cost per Request**:
```
Input tokens:  ______
Output tokens: ______
Cost per 1K input:  $______
Cost per 1K output: $______

Request cost = (Input/1000 × Input Rate) + (Output/1000 × Output Rate)
             = $______
```

#### Decomposed Approach

| Node | Input Tokens | Output Tokens | Frequency |
|------|--------------|---------------|-----------|
| Intent Classification | ______ | ______ | 100% |
| Specialist A | ______ | ______ | ___% |
| Specialist B | ______ | ______ | ___% |
| Quality Check | ______ | ______ | ___% |
| **Total Avg** | **______** | **______** | **100%** |

**Decomposed Cost per Request**:
```
= $______
```

---

### Step 3: Calculate Break-Even

```
Monolithic cost:    $______ per request
Decomposed cost:  $______ per request
Savings per request: $______ (___%)

Development overhead: ___ hours × $___/hour = $______
Maintenance overhead: $______/month

Break-even requests: $Overhead / $Savings = ______ requests
Expected monthly requests: ______
Payback period: ______ months
```

---

### Step 4: Qualitative Factors

Score each factor (1-5, 5 being best for decomposition):

| Factor | Monolithic | Decomposed | Weight |
|--------|------------|------------|--------|
| **Maintainability** | ___ | ___ | 20% |
| **Testability** | ___ | ___ | 15% |
| **Latency** | ___ | ___ | 25% |
| **Accuracy** | ___ | ___ | 20% |
| **Scalability** | ___ | ___ | 20% |
| **Weighted Score** | ___ | ___ | 100% |

**Decision**: Choose architecture with higher weighted score.

---

### Step 5: Risk Assessment

#### Decomposition Risks
- [ ] Routing errors (intent misclassification)
- [ ] Context loss between nodes
- [ ] Debugging complexity
- [ ] Deployment coordination

#### Mitigations
- Routing confidence threshold: ___%
- Fallback to monolithic when confidence < threshold
- Shared context store (Redis/pgvector)
- Comprehensive logging across nodes

---

## Decision Matrix

| Scenario | Recommendation | Example |
|----------|----------------|---------|
| Single intent, simple responses | **Monolithic** | FAQ bot |
| 2-3 intents, shared context | **Light decomposition** | Search + filter |
| 4+ intents, different expertise | **Full decomposition** | Customer support |
| Strict latency requirements | **Monolithic or cached decomposition** | Real-time trading |
| High accuracy requirements | **Decomposed with QA node** | Medical diagnosis |

---

## Implementation Checklist

### If Decomposing:
- [ ] Define intent taxonomy
- [ ] Design state schema (use `AgentState` template)
- [ ] Implement routing logic
- [ ] Add token budget tracking per node
- [ ] Create fallback paths
- [ ] Set up monitoring per node
- [ ] Document cost model

### If Monolithic:
- [ ] Optimize prompt for all use cases
- [ ] Implement response caching
- [ ] Add selective context compression
- [ ] Set up comprehensive logging
- [ ] Document when to reconsider decomposition

---

## Example Calculations

### Scenario: Customer Support Bot

**Intents**: Refund (30%), Order Status (40%), Tech Support (20%), General (10%)

**Monolithic**:
- Input: 2000 tokens (full policy context)
- Output: 400 tokens
- Model: gpt-4o-mini
- Cost: (2000/1000 × $0.00015) + (400/1000 × $0.0006) = $0.00054

**Decomposed**:
- Intent classifier: 200 in / 50 out → $0.00006
- Refund specialist (30%): 800 in / 300 out → $0.00030
- Order specialist (40%): 600 in / 200 out → $0.00021
- Tech specialist (20%): 1000 in / 400 out → $0.00039
- General (10%): 500 in / 200 out → $0.00020
- Quality check (100%): 300 in / 50 out → $0.000075

**Weighted average**: $0.00006 + (0.3×$0.00030) + (0.4×$0.00021) + (0.2×$0.00039) + (0.1×$0.00020) + $0.000075
                = $0.00006 + $0.00009 + $0.000084 + $0.000078 + $0.00002 + $0.000075
                = **$0.000407**

**Savings**: $0.00054 - $0.000407 = **$0.000133 per request (25% savings)**

**At 100K requests/month**: Save $13.30/month
**At 1M requests/month**: Save $133/month

---

## Quick Reference

### Token Counting Rules
- English text: ~4 characters per token
- Code: ~3 characters per token
- System prompt overhead: +50 tokens

### Pricing (as of 2026)
| Model | Input/1K | Output/1K |
|-------|----------|-----------|
| gpt-4o-mini | $0.00015 | $0.0006 |
| gpt-4o | $0.005 | $0.015 |
| gpt-4-5 | $0.075 | $0.15 |

### Decomposition Patterns
1. **Intent-based**: Route by user intent (customer support)
2. **Pipeline**: Sequential processing (doc review)
3. **Hierarchical**: Manager + workers (research agents)
4. **Conditional**: Dynamic routing based on confidence

---

## Next Steps

1. Complete this worksheet for your use case
2. Run A/B test with both architectures
3. Monitor: latency, cost, accuracy per intent
4. Iterate on decomposition granularity

**Template Location**: `templates/langgraph_decomposition.py`
**Cost Model**: `templates/cost_model_worksheet.md` (this file)
