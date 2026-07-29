// analytics-service/src/main/java/com/example/analytics/UserClient.java
// Consumer of POST /users API

package com.example.analytics;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.URI;

public class UserClient {

    private static final String BASE_URL = "https://api.example.com";
    private final HttpClient client = HttpClient.newHttpClient();

    public String createUser(String name, String email) throws Exception {
        String json = String.format(
            "{\"name\": \"%s\", \"email\": \"%s\"}",
            name, email
        );

        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(BASE_URL + "/users"))
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json))
            .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return response.body();
    }
}
