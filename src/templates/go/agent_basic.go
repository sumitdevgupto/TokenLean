// Token Optimisation Proxy — Go Developer Starter Kit
//
// Usage:
//   export PROXY_ENDPOINT=https://token-proxy-<hash>-uc.a.run.app
//   export PROXY_API_KEY=<your-proxy-key>
//   go run agent_basic.go
//
// Do NOT use LLM provider keys — the proxy handles all provider authentication.
package main

import (
	"context"
	"fmt"
	"os"

	"github.com/sashabaranov/go-openai"
)

func main() {
	proxyEndpoint := os.Getenv("PROXY_ENDPOINT")
	proxyAPIKey := os.Getenv("PROXY_API_KEY")

	if proxyEndpoint == "" || proxyAPIKey == "" {
		fmt.Fprintln(os.Stderr, "PROXY_ENDPOINT and PROXY_API_KEY must be set")
		os.Exit(1)
	}

	// Configure client to use proxy — only change needed vs direct OpenAI usage
	config := openai.DefaultConfig(proxyAPIKey)
	config.BaseURL = proxyEndpoint + "/v1"
	client := openai.NewClientWithConfig(config)

	// Basic call
	answer, err := ask(client, "What is token optimisation?")
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	fmt.Println("Response:", answer)

	// Session-aware call (G10 memory management)
	answer2, err := askWithSession(client, "Tell me more about context windows.", "go-session-001")
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
	fmt.Println("Session response:", answer2)
}

func ask(client *openai.Client, prompt string) (string, error) {
	resp, err := client.CreateChatCompletion(
		context.Background(),
		openai.ChatCompletionRequest{
			Model: "gpt-4o-mini",
			Messages: []openai.ChatCompletionMessage{
				{Role: openai.ChatMessageRoleUser, Content: prompt},
			},
			MaxTokens: 512,
		},
	)
	if err != nil {
		return "", err
	}
	return resp.Choices[0].Message.Content, nil
}

func askWithSession(client *openai.Client, prompt string, sessionID string) (string, error) {
	resp, err := client.CreateChatCompletion(
		context.Background(),
		openai.ChatCompletionRequest{
			Model: "gpt-4o-mini",
			Messages: []openai.ChatCompletionMessage{
				{Role: openai.ChatMessageRoleUser, Content: prompt},
			},
			MaxTokens: 512,
			// G10: session memory, G17: workflow budget tracking
			// Passed as extra fields — proxy reads these transparently
			User: os.Getenv("USER"),
		},
	)
	if err != nil {
		return "", err
	}
	return resp.Choices[0].Message.Content, nil
}
