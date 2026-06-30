# Java Developer Template

## Setup

```bash
export PROXY_ENDPOINT=https://token-proxy-<hash>-uc.a.run.app
export PROXY_API_KEY=<your-proxy-key>
mvn package
java -jar target/token-opt-java-template-1.0.0.jar
```

> **Never use LLM provider keys directly.** The proxy handles all provider authentication.

## Maven dependency

```xml
<dependency>
    <groupId>com.openai</groupId>
    <artifactId>openai-java</artifactId>
    <version>0.9.0</version>
</dependency>
```

## Key pattern

```java
OpenAIClient client = OpenAIOkHttpClient.builder()
    .apiKey(System.getenv("PROXY_API_KEY"))   // proxy key, not OpenAI key
    .baseUrl(System.getenv("PROXY_ENDPOINT") + "/v1")
    .build();
```

All optimisations (G1–G18) are transparent — your code doesn't change.
Pass `x_session_id` for multi-turn memory (G10) and `workflow_id` for budget tracking (G17).
