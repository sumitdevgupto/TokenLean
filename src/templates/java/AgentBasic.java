package com.example.tokenopt;

import com.openai.client.OpenAIClient;
import com.openai.client.okhttp.OpenAIOkHttpClient;
import com.openai.models.ChatCompletion;
import com.openai.models.ChatCompletionCreateParams;
import com.openai.models.ChatCompletionMessageParam;
import com.openai.models.ChatCompletionUserMessageParam;

import java.util.List;
import java.util.Map;

/**
 * Token Optimisation Proxy — Java Developer Starter Kit
 *
 * Replace PROXY_ENDPOINT and PROXY_API_KEY env vars with values from your platform team.
 * Do NOT use LLM provider keys — the proxy handles all provider authentication.
 *
 * Maven dependency: com.openai:openai-java:0.9.0+
 */
public class AgentBasic {

    private static final String PROXY_ENDPOINT = System.getenv("PROXY_ENDPOINT");
    private static final String PROXY_API_KEY   = System.getenv("PROXY_API_KEY");

    private final OpenAIClient client;

    public AgentBasic() {
        this.client = OpenAIOkHttpClient.builder()
            .apiKey(PROXY_API_KEY)
            .baseUrl(PROXY_ENDPOINT + "/v1")
            .build();
    }

    public String ask(String prompt) {
        ChatCompletionCreateParams params = ChatCompletionCreateParams.builder()
            .model("gpt-4o-mini")
            .messages(List.of(
                ChatCompletionMessageParam.ofUser(
                    ChatCompletionUserMessageParam.builder()
                        .content(prompt)
                        .build()
                )
            ))
            .maxTokens(512)
            .build();

        ChatCompletion completion = client.chat().completions().create(params);
        return completion.choices().get(0).message().content().orElse("");
    }

    public String askWithSession(String prompt, String sessionId) {
        ChatCompletionCreateParams params = ChatCompletionCreateParams.builder()
            .model("gpt-4o-mini")
            .messages(List.of(
                ChatCompletionMessageParam.ofUser(
                    ChatCompletionUserMessageParam.builder()
                        .content(prompt)
                        .build()
                )
            ))
            .maxTokens(512)
            // G10: session-aware memory management
            .putAdditionalBodyProperty("x_session_id", sessionId)
            // G17: workflow budget tracking
            .putAdditionalBodyProperty("workflow_id", "java-workflow-" + sessionId)
            .putAdditionalBodyProperty("user", System.getProperty("user.name", "java-dev"))
            .build();

        ChatCompletion completion = client.chat().completions().create(params);
        return completion.choices().get(0).message().content().orElse("");
    }

    public static void main(String[] args) {
        AgentBasic agent = new AgentBasic();
        System.out.println(agent.ask("What is machine learning?"));
        System.out.println(agent.askWithSession("Summarise recent AI trends.", "session-001"));
    }
}
