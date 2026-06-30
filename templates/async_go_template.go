// Token Optimisation Proxy — Async Go Template
//
// Demonstrates goroutine patterns for concurrent LLM requests
// with token optimization headers and batch processing.
//
// Requirements:
//   go get github.com/go-resty/resty/v2
package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	"github.com/go-resty/resty/v2"
)

// TokenOptState represents the inter-agent state from x-token-opt-state header
type TokenOptState struct {
	TokenBudgetRemaining   int     `json:"token_budget_remaining"`
	WorkflowTurn           int     `json:"workflow_turn"`
	MaxIterations          int     `json:"max_iterations"`
	ConfidenceScore        *float64 `json:"confidence_score,omitempty"`
	WallClockElapsedSeconds *float64 `json:"wall_clock_elapsed_seconds,omitempty"`
	StopReason             *string  `json:"stop_reason,omitempty"`
	WorkflowID             string   `json:"workflow_id"`
	TemplateID             string   `json:"template_id"`
}

// ToHeaderValue serializes state to base64-encoded JSON
func (s *TokenOptState) ToHeaderValue() string {
	jsonBytes, _ := json.Marshal(s)
	return base64.StdEncoding.EncodeToString(jsonBytes)
}

// TokenOptStateFromHeader parses state from header value
func TokenOptStateFromHeader(headerValue string) (*TokenOptState, error) {
	decoded, err := base64.StdEncoding.DecodeString(headerValue)
	if err != nil {
		return nil, err
	}
	
	var state TokenOptState
	if err := json.Unmarshal(decoded, &state); err != nil {
		return nil, err
	}
	return &state, nil
}

// ChatResponse represents API response with token optimization
type ChatResponse struct {
	Content    string         `json:"content"`
	TokenCount int            `json:"token_count"`
	State      *TokenOptState `json:"state,omitempty"`
	Usage      struct {
		TotalTokens int `json:"total_tokens"`
	} `json:"usage"`
}

// AsyncClient provides async LLM operations
type AsyncClient struct {
	BaseURL    string
	APIKey     string
	HTTPClient *resty.Client
}

// NewAsyncClient creates configured client
func NewAsyncClient(baseURL, apiKey string) *AsyncClient {
	client := resty.New()
	client.SetTimeout(60 * time.Second)
	client.SetHeader("Authorization", "Bearer "+apiKey)
	client.SetHeader("Content-Type", "application/json")
	
	return &AsyncClient{
		BaseURL:    baseURL,
		APIKey:     apiKey,
		HTTPClient: client,
	}
}

// ChatAsync performs single async request with state tracking
func (c *AsyncClient) ChatAsync(message string, state *TokenOptState) <-chan *ChatResponse {
	resultChan := make(chan *ChatResponse, 1)
	
	go func() {
		defer close(resultChan)
		
		request := map[string]interface{}{
			"model": "gpt-4o-mini",
			"messages": []map[string]string{
				{"role": "user", "content": message},
			},
		}
		
		// Add tracking IDs
		if state != nil {
			request["workflow_id"] = state.WorkflowID
			request["template_id"] = state.TemplateID
		}
		
		req := c.HTTPClient.R().SetBody(request)
		
		// Add x-token-opt-state header
		if state != nil {
			req.SetHeader("x-token-opt-state", state.ToHeaderValue())
		}
		
		var response ChatResponse
		resp, err := req.
			SetResult(&response).
			Post(c.BaseURL + "/v1/chat/completions")
		
		if err != nil {
			fmt.Printf("ChatAsync error: %v\n", err)
			resultChan <- nil
			return
		}
		
		// Parse x-token-opt-state from response
		if stateHeader := resp.Header().Get("x-token-opt-state"); stateHeader != "" {
			if returnedState, err := TokenOptStateFromHeader(stateHeader); err == nil {
				response.State = returnedState
			}
		}
		
		response.TokenCount = response.Usage.TotalTokens
		resultChan <- &response
	}()
	
	return resultChan
}

// TOONLegend for batch compression
type TOONLegend struct {
	mu            sync.RWMutex
	substitutions map[string]string
}

// NewTOONLegend creates legend
func NewTOONLegend() *TOONLegend {
	return &TOONLegend{
		substitutions: make(map[string]string),
	}
}

// AddSubstitution adds code -> value mapping
func (l *TOONLegend) AddSubstitution(code, value string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.substitutions[code] = value
}

// Get retrieves value by code
func (l *TOONLegend) Get(code string) (string, bool) {
	l.mu.RLock()
	defer l.mu.RUnlock()
	val, ok := l.substitutions[code]
	return val, ok
}

// CompressWithTOON applies TOON compression to messages
func CompressWithTOON(messages []string, state *TokenOptState) ([]string, *TOONLegend) {
	legend := NewTOONLegend()
	
	// Find common prefix
	if len(messages) == 0 {
		return messages, legend
	}
	
	commonPrefix := longestCommonPrefix(messages)
	if len(commonPrefix) > 50 {
		legend.AddSubstitution("#P", commonPrefix)
	}
	
	// Add state to legend
	if state != nil {
		legend.AddSubstitution("#S", state.ToHeaderValue())
	}
	
	// Compress messages
	compressed := make([]string, len(messages))
	for i, msg := range messages {
		compressed[i] = msg
		if len(commonPrefix) > 50 {
			// Simple prefix replacement (production would be smarter)
			if len(msg) > len(commonPrefix) && msg[:len(commonPrefix)] == commonPrefix {
				compressed[i] = "#P" + msg[len(commonPrefix):]
			}
		}
	}
	
	return compressed, legend
}

func longestCommonPrefix(strs []string) string {
	if len(strs) == 0 {
		return ""
	}
	
	prefix := strs[0]
	for _, s := range strs[1:] {
		for len(s) < len(prefix) || s[:len(prefix)] != prefix {
			if len(prefix) == 0 {
				return ""
			}
			prefix = prefix[:len(prefix)-1]
		}
	}
	return prefix
}

// ChatBatchAsync performs parallel batch requests
func (c *AsyncClient) ChatBatchAsync(messages []string, state *TokenOptState) <-chan []*ChatResponse {
	resultChan := make(chan []*ChatResponse, 1)
	
	go func() {
		defer close(resultChan)
		
		// Compress with TOON
		compressed, legend := CompressWithTOON(messages, state)
		_ = legend // Use for decompression later
		
		// Create worker pool
		const maxConcurrency = 10
		semaphore := make(chan struct{}, maxConcurrency)
		
		var wg sync.WaitGroup
		results := make([]*ChatResponse, len(compressed))
		var mu sync.Mutex
		
		for i, msg := range compressed {
			wg.Add(1)
			go func(index int, message string) {
				defer wg.Done()
				
				semaphore <- struct{}{} // Acquire
				defer func() { <-semaphore }() // Release
				
				// Decompress for request (simplified)
				fullMessage := decompressMessage(message, legend)
				
				resp := <-c.ChatAsync(fullMessage, state)
				
				mu.Lock()
				results[index] = resp
				mu.Unlock()
			}(i, msg)
		}
		
		wg.Wait()
		resultChan <- results
	}()
	
	return resultChan
}

func decompressMessage(msg string, legend *TOONLegend) string {
	// Simplified - production would handle all substitutions
	if val, ok := legend.Get("#P"); ok && len(msg) > 2 && msg[:2] == "#P" {
		return val + msg[2:]
	}
	return msg
}

// AgentResult from sub-agent
type AgentResult struct {
	Specialization string
	Response       *ChatResponse
	State          *TokenOptState
}

// MultiAgentResult aggregates sub-agent results
type MultiAgentResult struct {
	Results   []AgentResult
	TotalCost float64
}

// SpawnSubAgent creates a sub-agent with allocated budget
func (c *AsyncClient) SpawnSubAgent(query, specialization string, parentState *TokenOptState) <-chan AgentResult {
	resultChan := make(chan AgentResult, 1)
	
	go func() {
		defer close(resultChan)
		
		// Allocate half budget to sub-agent
		budget := parentState.TokenBudgetRemaining / 2
		subState := &TokenOptState{
			TokenBudgetRemaining: budget,
			WorkflowTurn:         1,
			MaxIterations:        5,
			WorkflowID:           parentState.WorkflowID,
			TemplateID:           specialization,
		}
		
		prompt := fmt.Sprintf(
			"[Specialization: %s]\nAnalyze this query: %s",
			specialization, query,
		)
		
		resp := <-c.ChatAsync(prompt, subState)
		
		resultChan <- AgentResult{
			Specialization: specialization,
			Response:       resp,
			State:          subState,
		}
	}()
	
	return resultChan
}

// RunMultiAgentWorkflow executes parallel sub-agents
func (c *AsyncClient) RunMultiAgentWorkflow(query string, specializations []string) *MultiAgentResult {
	initialState := &TokenOptState{
		TokenBudgetRemaining: 4000,
		WorkflowTurn:         1,
		MaxIterations:        10,
		WorkflowID:           "multi-agent-workflow",
		TemplateID:           "decomposition",
	}
	
	// Spawn all sub-agents concurrently
	resultChans := make([]<-chan AgentResult, len(specializations))
	for i, spec := range specializations {
		resultChans[i] = c.SpawnSubAgent(query, spec, initialState)
	}
	
	// Collect results with timeout
	results := make([]AgentResult, 0, len(specializations))
	timeout := time.After(30 * time.Second)
	
	for _, ch := range resultChans {
		select {
		case result := <-ch:
			if result.Response != nil {
				results = append(results, result)
			}
		case <-timeout:
			// Timeout - continue with partial results
			fmt.Println("Sub-agent timeout - returning partial results")
		}
	}
	
	// Calculate total cost
	totalCost := 0.0
	for _, r := range results {
		if r.Response != nil {
			totalCost += float64(r.Response.TokenCount) * 0.00015 // Approximate cost
		}
	}
	
	return &MultiAgentResult{
		Results:   results,
		TotalCost: totalCost,
	}
}

// Example usage
func main() {
	client := NewAsyncClient("http://localhost:8080", "test-key")
	
	// Single async request
	fmt.Println("Single request:")
	state := &TokenOptState{
		TokenBudgetRemaining: 2000,
		WorkflowTurn:         1,
		MaxIterations:        5,
	}
	
	resp := <-client.ChatAsync("What is token optimization?", state)
	if resp != nil {
		fmt.Printf("Response: %s (tokens: %d)\n", resp.Content, resp.TokenCount)
	}
	
	// Multi-agent workflow
	fmt.Println("\nMulti-agent workflow:")
	specializations := []string{"technical", "business", "legal"}
	result := client.RunMultiAgentWorkflow(
		"Review this API design proposal",
		specializations,
	)
	
	fmt.Printf("Total cost: $%.4f\n", result.TotalCost)
	for _, r := range result.Results {
		if r.Response != nil {
			fmt.Printf("- %s agent: %d tokens\n", r.Specialization, r.Response.TokenCount)
		}
	}
}
