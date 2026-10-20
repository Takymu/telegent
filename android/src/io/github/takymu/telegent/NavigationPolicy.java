package io.github.takymu.telegent;

import java.net.URI;
import java.net.URISyntaxException;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.Locale;

/** URL decisions shared by navigation and credential-bearing file downloads. */
public final class NavigationPolicy {
    public static final String DEFAULT_ENTRY = "https://takymu.github.io/telegent/";

    public static String origin(String value) {
        try {
            URI uri = new URI(value);
            String host = uri.getHost();
            if (!"https".equalsIgnoreCase(uri.getScheme()) || host == null || uri.getRawUserInfo() != null)
                return null;
            int port = uri.getPort();
            if (port < -1 || port == 0 || port > 65535) return null;
            return "https://" + host.toLowerCase(Locale.ROOT) + (port == -1 || port == 443 ? "" : ":" + port);
        } catch (URISyntaxException | NullPointerException error) {
            return null;
        }
    }

    public static boolean sameOrigin(String left, String right) {
        String first = origin(left);
        return first != null && first.equals(origin(right));
    }

    public static boolean launcher(String value, String entry) {
        try {
            URI uri = new URI(value), base = new URI(entry);
            return sameOrigin(value, entry) && path(uri).equals(path(base));
        } catch (URISyntaxException | NullPointerException error) {
            return false;
        }
    }

    private static String path(URI uri) {
        String result = uri.getPath();
        return result == null || result.isEmpty() ? "/" : result;
    }

    public static String entry(String value) {
        if (value == null) return null;
        String result = value.trim();
        try {
            URI uri = new URI(result);
            if (origin(result) == null || uri.getRawQuery() != null || uri.getRawFragment() != null) return null;
            return result;
        } catch (URISyntaxException error) {
            return null;
        }
    }

    public static boolean discoveredServer(String value) {
        try {
            URI uri = new URI(value);
            String host = uri.getHost();
            return origin(value) != null && host != null && host.toLowerCase(Locale.ROOT).endsWith(".trycloudflare.com")
                && uri.getPort() == -1 && "/".equals(path(uri));
        } catch (URISyntaxException | NullPointerException error) {
            return false;
        }
    }

    public static boolean attachment(String value, String server) {
        try {
            URI uri = new URI(value);
            return sameOrigin(value, server) && path(uri).matches("/api/attachments/[0-9]+")
                && uri.getRawFragment() == null;
        } catch (URISyntaxException | NullPointerException error) {
            return false;
        }
    }

    public static String publicUrl(String value) {
        try {
            URI uri = new URI(value);
            if (origin(value) == null) return null;
            return new URI(uri.getScheme(), null, uri.getHost(), uri.getPort(), path(uri), null, null).toString();
        } catch (URISyntaxException | NullPointerException error) {
            return null;
        }
    }

    public static String downloadName(String disposition, String fallback) {
        String name = fallback;
        // The server uses RFC 5987 names; older WebViews only understand filename=.
        if (disposition != null) {
            for (String part : disposition.split(";")) {
                String field = part.trim();
                if (field.toLowerCase(Locale.ROOT).startsWith("filename*=utf-8''")) {
                    try { name = URLDecoder.decode(field.substring(17).replace("+", "%2B"), "UTF-8"); }
                    catch (java.io.UnsupportedEncodingException | IllegalArgumentException error) { /* Keep the fallback. */ }
                    break;
                }
            }
        }
        String result = name == null ? "file" : name.replaceAll("[\\\\/\\p{Cntrl}]", "_").trim();
        if (result.isEmpty() || result.equals(".") || result.equals("..")) result = "file";
        while (result.getBytes(StandardCharsets.UTF_8).length > 200)
            result = result.substring(0, result.offsetByCodePoints(result.length(), -1));
        return result;
    }
}
