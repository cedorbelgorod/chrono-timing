package com.chrono.timer;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.SharedPreferences;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Bundle;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.Toast;

import java.net.HttpURLConnection;
import java.net.URL;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.util.LinkedHashSet;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/**
 * Хронометраж — мобильное приложение.
 *
 * Строго онлайн: открывает веб-интерфейс хронометража на сервере,
 * все проверки выполняются на сервере.
 *
 * Адрес сервера: приложение по очереди проверяет известные адреса
 * (сохранённый, затем встроенные), открывает первый ответивший.
 * Если не ответил никто — спрашивает адрес у пользователя и запоминает его.
 *
 * Внутри приложения нет переходов на другие разделы сайта.
 */
public class MainActivity extends Activity {

    private static final String PREFS = "chrono";
    private static final String KEY_SERVER = "server_url";

    /** Встроенные адреса: Ethernet (локальная сеть) и точка доступа Wi-Fi этого ПК. */
    private static final String[] DEFAULT_SERVERS = {
            "http://192.168.88.58:5000",
            "http://192.168.111.1:5000"
    };

    private static final String START_PATH = "/?embed=1";
    private static final String[] ALLOWED_PATHS = {"/", "/c/", "/manual", "/login", "/logout", "/static/"};

    private WebView web;
    private SharedPreferences prefs;
    private String baseUrl;          // рабочий адрес без слэша на конце
    private boolean dialogShowing;   // чтобы не плодить диалоги
    private boolean pageLoaded;      // была ли уже успешная загрузка

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        web = new WebView(this);
        web.setLayoutParams(new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setSupportMultipleWindows(false);
        s.setJavaScriptCanOpenWindowsAutomatically(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);

        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);
        cm.setAcceptThirdPartyCookies(web, true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.proceed(); // самоподписанный сертификат в локальной сети
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return !isAllowed(request.getUrl().toString());
            }

            @Override
            @SuppressWarnings("deprecation")
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return !isAllowed(url);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request != null && request.isForMainFrame()) onLoadFailed();
            }

            @Override
            @SuppressWarnings("deprecation")
            public void onReceivedError(WebView view, int errorCode, String description, String failingUrl) {
                onLoadFailed();
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                pageLoaded = true;
            }
        });
        web.setWebChromeClient(new WebChromeClient());

        setContentView(web);

        if (state != null && state.getString("url") != null) {
            String saved = state.getString("url");
            baseUrl = baseOf(saved);
            web.loadUrl(saved);
        } else {
            connect();
        }
    }

    /** Подбор адреса: сохранённый → встроенные → диалог. */
    private void connect() {
        showSplash("Поиск сервера…");
        new Thread(new Runnable() {
            public void run() {
                final String found = findServer();
                runOnUiThread(new Runnable() {
                    public void run() {
                        if (found != null) {
                            baseUrl = found;
                            saveServer(found);
                            openStart();
                        } else {
                            askServer(null);
                        }
                    }
                });
            }
        }).start();
    }

    private String findServer() {
        LinkedHashSet<String> candidates = new LinkedHashSet<String>();
        String saved = prefs.getString(KEY_SERVER, null);
        if (saved != null && !saved.isEmpty()) candidates.add(saved);
        for (String d : DEFAULT_SERVERS) candidates.add(d);
        for (String c : candidates) {
            if (probe(c)) return c;
        }
        return null;
    }

    /** Быстрая проверка: отвечает ли сервер по адресу. */
    private boolean probe(String base) {
        HttpURLConnection conn = null;
        try {
            URL u = new URL(base + "/login");
            conn = (HttpURLConnection) u.openConnection();
            if (conn instanceof HttpsURLConnection) {
                HttpsURLConnection h = (HttpsURLConnection) conn;
                h.setSSLSocketFactory(trustAll().getSocketFactory());
                h.setHostnameVerifier(new javax.net.ssl.HostnameVerifier() {
                    public boolean verify(String host, javax.net.ssl.SSLSession session) { return true; }
                });
            }
            conn.setRequestMethod("GET");
            conn.setConnectTimeout(1500);
            conn.setReadTimeout(2500);
            conn.setInstanceFollowRedirects(false);
            int code = conn.getResponseCode();
            return code > 0 && code < 500;
        } catch (Exception e) {
            return false;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private static SSLContext trustAll() throws Exception {
        SSLContext ctx = SSLContext.getInstance("TLS");
        ctx.init(null, new TrustManager[]{new X509TrustManager() {
            public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            public void checkClientTrusted(X509Certificate[] chain, String authType) { }
            public void checkServerTrusted(X509Certificate[] chain, String authType) { }
        }}, new SecureRandom());
        return ctx;
    }

    private void showSplash(String text) {
        String html = "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
                + "<style>body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;"
                + "font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#f4f6f8;color:#374151}"
                + "</style></head><body><div>" + text + "</div></body></html>";
        web.loadDataWithBaseURL(null, html, "text/html", "UTF-8", null);
    }

    private void openStart() {
        pageLoaded = false;
        web.loadUrl(baseUrl + START_PATH);
    }

    private void onLoadFailed() {
        if (dialogShowing) return;
        askServer("Нет связи с сервером " + (baseUrl == null ? "" : baseUrl)
                + ".\nПроверьте, что телефон в той же сети, и укажите адрес сервера.");
    }

    /** Диалог ввода адреса сервера. */
    private void askServer(String message) {
        dialogShowing = true;
        final EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        String prefill = baseUrl != null ? baseUrl : prefs.getString(KEY_SERVER, DEFAULT_SERVERS[0]);
        input.setText(prefill);
        input.setSelection(input.getText().length());

        AlertDialog.Builder b = new AlertDialog.Builder(this)
                .setTitle("Адрес сервера")
                .setView(input)
                .setCancelable(false)
                .setPositiveButton("Подключиться", new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface d, int w) {
                        dialogShowing = false;
                        tryConnect(normalize(input.getText().toString()));
                    }
                })
                .setNegativeButton("Отмена", new DialogInterface.OnClickListener() {
                    public void onClick(DialogInterface d, int w) {
                        dialogShowing = false;
                        if (baseUrl == null) finish();
                    }
                });
        if (message != null && !message.isEmpty()) b.setMessage(message);
        b.show();
    }

    /** Проверяет введённый адрес и открывает приложение. */
    private void tryConnect(final String candidate) {
        showSplash("Подключение к " + candidate + "…");
        new Thread(new Runnable() {
            public void run() {
                final boolean ok = probe(candidate);
                runOnUiThread(new Runnable() {
                    public void run() {
                        if (ok) {
                            baseUrl = candidate;
                            saveServer(candidate);
                            openStart();
                        } else {
                            Toast.makeText(MainActivity.this,
                                    "Сервер не отвечает: " + candidate, Toast.LENGTH_LONG).show();
                            askServer("Сервер не отвечает. Проверьте адрес и сеть.");
                        }
                    }
                });
            }
        }).start();
    }

    /** Приводит ввод к виду http://host:port без слэша на конце. */
    private static String normalize(String raw) {
        String s = raw == null ? "" : raw.trim();
        if (s.isEmpty()) return DEFAULT_SERVERS[0];
        if (!s.startsWith("http://") && !s.startsWith("https://")) s = "http://" + s;
        while (s.endsWith("/")) s = s.substring(0, s.length() - 1);
        return s;
    }

    private static String baseOf(String url) {
        try {
            Uri u = Uri.parse(url);
            String scheme = u.getScheme() == null ? "http" : u.getScheme();
            String host = u.getHost();
            int port = u.getPort();
            if (host == null) return DEFAULT_SERVERS[0];
            return scheme + "://" + host + (port > 0 ? ":" + port : "");
        } catch (Exception e) {
            return DEFAULT_SERVERS[0];
        }
    }

    private void saveServer(String url) {
        prefs.edit().putString(KEY_SERVER, url).apply();
    }

    /** Разрешены только страницы хронометража и вход; всё остальное — блокируем. */
    private boolean isAllowed(String url) {
        if (url == null || baseUrl == null) return false;
        try {
            Uri u = Uri.parse(url);
            String host = u.getHost();
            String baseHost = Uri.parse(baseUrl).getHost();
            if (host != null && baseHost != null && !host.equalsIgnoreCase(baseHost)) return false;

            String path = u.getPath();
            if (path == null || path.isEmpty()) return false;
            if (path.equals("/")) return true;   // выбор соревнования
            for (String allowed : ALLOWED_PATHS) {
                if (allowed.equals("/")) continue;
                if (path.equals(allowed) || path.startsWith(allowed)) return true;
            }
            return false;
        } catch (Exception e) {
            return false;
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        if (web != null && web.getUrl() != null) out.putString("url", web.getUrl());
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }
}
