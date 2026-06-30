# Go Developer Template

## Setup

```bash
export PROXY_ENDPOINT=https://token-proxy-<hash>-uc.a.run.app
export PROXY_API_KEY=<your-proxy-key>
go mod init myagent
go get github.com/sashabaranov/go-openai
go run agent_basic.go
```

> **Never use LLM provider keys directly.** The proxy handles all provider authentication.

## Key pattern

```go
config := openai.DefaultConfig(os.Getenv("PROXY_API_KEY"))  // proxy key, not OpenAI key
config.BaseURL = os.Getenv("PROXY_ENDPOINT") + "/v1"
client := openai.NewClientWithConfig(config)
```

All G1–G18 optimisations are transparent. Pass `User` field for per-user savings tracking.
