/**
 * Token Optimisation Proxy — Async Java Template
 * 
 * Demonstrates CompletableFuture patterns for concurrent LLM requests
 * with token optimization headers and batch processing.
 * 
 * Requirements:
 *   implementation 'com.squareup.okhttp3:okhttp:4.12.0'
 *   implementation 'com.fasterxml.jackson.core:jackson-databind:2.15.2'
 */
package com.example.tokenopt;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import okhttp3.*;

import java.io.IOException;
import java.util.Base64;
import java.util.List;
import java.util.concurrent.*;
import java.util.stream.Collectors;

public class AsyncTokenOptimisationClient {
    
    private final OkHttpClient httpClient;
    private final ObjectMapper objectMapper;
    private final String baseUrl;
    private final String apiKey;
    
    public AsyncTokenOptimisationClient(String baseUrl, String apiKey) {
        this.baseUrl = baseUrl;
        this.apiKey = apiKey;
        this.objectMapper = new ObjectMapper();
        
        // Configure connection pool for concurrent requests
        this.httpClient = new OkHttpClient.Builder()
            .connectionPool(new ConnectionPool(10, 5, TimeUnit.MINUTES))
            .dispatcher(new Dispatcher(Executors.newFixedThreadPool(20)))
            .readTimeout(60, TimeUnit.SECONDS)
            .build();
    }
    
    /**
     * Single async request with x-token-opt-state header support.
     */
    public CompletableFuture<ChatResponse> chatAsync(String message, TokenOptState state) {
        return CompletableFuture.supplyAsync(() -> {
            try {
                ObjectNode request = objectMapper.createObjectNode();
                request.put("model", "gpt-4o-mini");
                request.putArray("messages")
                    .add(objectMapper.createObjectNode()
                        .put("role", "user")
                        .put("content", message));
                
                // Add workflow/template IDs for optimization tracking
                if (state != null) {
                    request.put("workflow_id", state.workflowId);
                    request.put("template_id", state.templateId);
                }
                
                RequestBody body = RequestBody.create(
                    request.toString(),
                    MediaType.parse("application/json")
                );
                
                Request.Builder requestBuilder = new Request.Builder()
                    .url(baseUrl + "/v1/chat/completions")
                    .header("Authorization", "Bearer " + apiKey)
                    .header("Content-Type", "application/json")
                    .post(body);
                
                // Add x-token-opt-state header for multi-turn workflows
                if (state != null && state.toHeaderValue() != null) {
                    requestBuilder.header("x-token-opt-state", state.toHeaderValue());
                }
                
                try (Response response = httpClient.newCall(requestBuilder.build()).execute()) {
                    if (!response.isSuccessful()) {
                        throw new IOException("API error: " + response);
                    }
                    
                    JsonNode json = objectMapper.readTree(response.body().string());
                    
                    // Parse x-token-opt-state from response if present
                    String responseState = response.header("x-token-opt-state");
                    TokenOptState returnedState = responseState != null 
                        ? TokenOptState.fromHeaderValue(responseState) 
                        : null;
                    
                    return new ChatResponse(
                        json.path("choices").get(0).path("message").path("content").asText(),
                        json.path("usage").path("total_tokens").asInt(),
                        returnedState
                    );
                }
            } catch (Exception e) {
                throw new CompletionException(e);
            }
        });
    }
    
    /**
     * Parallel batch processing with TOON (Token-Optimized Object Notation).
     * Compresses repeated payloads using code substitution pattern.
     */
    public CompletableFuture<List<ChatResponse>> chatBatchAsync(
        List<String> messages,
        TokenOptState state
    ) {
        // Apply TOON compression for batch requests
        TOONBatchRequest batchRequest = compressWithTOON(messages, state);
        
        return CompletableFuture.supplyAsync(() -> {
            try {
                RequestBody body = RequestBody.create(
                    objectMapper.writeValueAsString(batchRequest),
                    MediaType.parse("application/json")
                );
                
                Request request = new Request.Builder()
                    .url(baseUrl + "/v1/chat/completions/batch")
                    .header("Authorization", "Bearer " + apiKey)
                    .header("Content-Type", "application/json")
                    .header("X-TOON-Version", "1.0")
                    .post(body)
                    .build();
                
                try (Response response = httpClient.newCall(request).execute()) {
                    JsonNode json = objectMapper.readTree(response.body().string());
                    
                    // Decompress responses
                    return decompressTOONResponses(json, batchRequest.legend);
                }
            } catch (Exception e) {
                throw new CompletionException(e);
            }
        });
    }
    
    /**
     * TOON (Token-Optimized Object Notation) compression.
     * Replaces repeated content with short codes.
     */
    private TOONBatchRequest compressWithTOON(List<String> messages, TokenOptState state) {
        // Find common prefixes/patterns
        String commonPrefix = findLongestCommonPrefix(messages);
        
        TOONLegend legend = new TOONLegend();
        if (commonPrefix.length() > 50) {
            legend.addSubstitution("#P", commonPrefix);
        }
        
        // Add state to legend
        if (state != null) {
            legend.addSubstitution("#S", state.toHeaderValue());
        }
        
        // Compress messages
        List<String> compressed = messages.stream()
            .map(msg -> commonPrefix.length() > 50 
                ? msg.replace(commonPrefix, "#P") 
                : msg)
            .map(msg -> state != null 
                ? msg.replace(state.toHeaderValue(), "#S") 
                : msg)
            .collect(Collectors.toList());
        
        return new TOONBatchRequest(compressed, legend, state);
    }
    
    private String findLongestCommonPrefix(List<String> strings) {
        if (strings.isEmpty()) return "";
        String prefix = strings.get(0);
        for (String s : strings) {
            while (!s.startsWith(prefix)) {
                prefix = prefix.substring(0, prefix.length() - 1);
                if (prefix.isEmpty()) break;
            }
        }
        return prefix;
    }
    
    private List<ChatResponse> decompressTOONResponses(JsonNode compressed, TOONLegend legend) {
        // Apply decompression using legend
        // Implementation would reverse the compression
        return List.of(); // Simplified
    }
    
    /**
     * Concurrent multi-agent workflow with budget tracking.
     */
    public CompletableFuture<MultiAgentResult> runMultiAgentWorkflow(
        String userQuery,
        List<String> agentSpecializations
    ) {
        TokenOptState initialState = new TokenOptState(
            4000,  // token_budget_remaining
            1,     // workflow_turn
            10,    // max_iterations
            null,  // confidence_score
            null,  // wall_clock_elapsed_seconds
            null   // stop_reason
        );
        
        // Spawn parallel sub-agent requests
        List<CompletableFuture<AgentResult>> agentFutures = agentSpecializations.stream()
            .map(specialization -> spawnSubAgent(userQuery, specialization, initialState))
            .collect(Collectors.toList());
        
        // Wait for all agents with timeout
        return CompletableFuture.allOf(agentFutures.toArray(new CompletableFuture[0]))
            .thenApply(v -> {
                List<AgentResult> results = agentFutures.stream()
                    .map(CompletableFuture::join)
                    .collect(Collectors.toList());
                
                // Aggregate results
                return new MultiAgentResult(results, calculateTotalCost(results));
            })
            .orTimeout(30, TimeUnit.SECONDS)
            .exceptionally(ex -> {
                // Handle timeout - return partial results
                return new MultiAgentResult(
                    agentFutures.stream()
                        .filter(f -> f.isDone() && !f.isCompletedExceptionally())
                        .map(CompletableFuture::join)
                        .collect(Collectors.toList()),
                    0.0
                );
            });
    }
    
    private CompletableFuture<AgentResult> spawnSubAgent(
        String query, 
        String specialization,
        TokenOptState parentState
    ) {
        // Allocate budget to sub-agent
        TokenOptState subState = new TokenOptState(
            parentState.tokenBudgetRemaining / 2,  // Allocate half budget
            1,
            5,
            null, null, null
        );
        
        String prompt = String.format(
            "[Specialization: %s]\nAnalyze this query and provide domain-specific insights: %s",
            specialization, query
        );
        
        return chatAsync(prompt, subState)
            .thenApply(response -> new AgentResult(specialization, response, subState));
    }
    
    private double calculateTotalCost(List<AgentResult> results) {
        return results.stream()
            .mapToDouble(r -> r.response.tokenCount() * 0.00015)  // Approximate cost
            .sum();
    }
    
    // Data classes
    public record ChatResponse(String content, int tokenCount, TokenOptState returnedState) {}
    public record AgentResult(String specialization, ChatResponse response, TokenOptState state) {}
    public record MultiAgentResult(List<AgentResult> results, double totalCost) {}
    
    public static class TokenOptState {
        public final int tokenBudgetRemaining;
        public final int workflowTurn;
        public final int maxIterations;
        public final Double confidenceScore;
        public final Double wallClockElapsedSeconds;
        public final String stopReason;
        public String workflowId = "default";
        public String templateId = "default";
        
        public TokenOptState(Integer budget, Integer turn, Integer maxIter,
                            Double confidence, Double elapsed, String stop) {
            this.tokenBudgetRemaining = budget != null ? budget : 4000;
            this.workflowTurn = turn != null ? turn : 1;
            this.maxIterations = maxIter != null ? maxIter : 5;
            this.confidenceScore = confidence;
            this.wallClockElapsedSeconds = elapsed;
            this.stopReason = stop;
        }
        
        public String toHeaderValue() {
            try {
                ObjectMapper mapper = new ObjectMapper();
                String json = mapper.writeValueAsString(this);
                return Base64.getEncoder().encodeToString(json.getBytes());
            } catch (Exception e) {
                return null;
            }
        }
        
        public static TokenOptState fromHeaderValue(String headerValue) {
            try {
                byte[] decoded = Base64.getDecoder().decode(headerValue);
                ObjectMapper mapper = new ObjectMapper();
                return mapper.readValue(decoded, TokenOptState.class);
            } catch (Exception e) {
                return null;
            }
        }
    }
    
    private static class TOONLegend {
        private final java.util.Map<String, String> substitutions = new java.util.HashMap<>();
        
        void addSubstitution(String code, String value) {
            substitutions.put(code, value);
        }
    }
    
    private record TOONBatchRequest(List<String> messages, TOONLegend legend, TokenOptState state) {}
}
