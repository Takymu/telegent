package io.github.takymu.telegent;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.res.Configuration;
import android.graphics.Color;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.PermissionRequest;
import android.webkit.SslErrorHandler;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.PopupMenu;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLConnection;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** First Android client: the existing server UI, with native file and lifecycle support. */
public final class MainActivity extends Activity {
    private static final int PICK_FILES = 10, SAVE_FILE = 11;
    private static final long MAX_FILE_BYTES = 512L * 1024 * 1024;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService files = Executors.newSingleThreadExecutor();
    private WebView web;
    private LinearLayout root, brand, errorPanel;
    private TextView title;
    private Button menu;
    private ProgressBar progress;
    private SharedPreferences preferences;
    private String entry, serverOrigin;
    private boolean discovering, pageFailed, destroyed, downloadBusy;
    private ValueCallback<Uri[]> fileSelection;
    private volatile boolean downloadCancelled;
    private volatile HttpURLConnection downloadConnection;
    private File downloadedFile;
    private AlertDialog downloadDialog;
    private AppUpdates updates;
    private NativePush push;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        SharedFilesProvider.cleanup(this);
        File[] stale = getCacheDir().listFiles((directory, name) -> name.startsWith("telegent-download-") && name.endsWith(".part"));
        if (stale != null) for (File file : stale) file.delete();
        preferences = getSharedPreferences("connection", MODE_PRIVATE);
        entry = preferences.getString("entry", NavigationPolicy.DEFAULT_ENTRY);
        if (NavigationPolicy.entry(entry) == null) entry = NavigationPolicy.DEFAULT_ENTRY;
        serverOrigin = preferences.getString("server_origin", null);
        if (serverOrigin == null && !NavigationPolicy.launcher(entry, NavigationPolicy.DEFAULT_ENTRY))
            serverOrigin = NavigationPolicy.origin(entry);

        root = new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL);
        brand = new LinearLayout(this); brand.setGravity(Gravity.CENTER_VERTICAL); brand.setPadding(dp(12), 0, 0, 0);
        title = new TextView(this); title.setText(R.string.tagline); title.setTextSize(13); title.setMaxLines(1);
        title.setEllipsize(android.text.TextUtils.TruncateAt.END);
        brand.addView(title, new LinearLayout.LayoutParams(0, dp(44), 1)); title.setGravity(Gravity.CENTER_VERTICAL);
        menu = new Button(this); menu.setText("⋮"); menu.setTextSize(22); menu.setContentDescription(getString(R.string.app_options));
        menu.setPadding(0, 0, 0, 0); menu.setBackgroundColor(Color.TRANSPARENT);
        brand.addView(menu, new LinearLayout.LayoutParams(dp(44), dp(44))); menu.setOnClickListener(view -> showOptions());
        root.addView(brand);
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100); root.addView(progress, new LinearLayout.LayoutParams(-1, dp(3)));
        FrameLayout content = new FrameLayout(this);
        web = new WebView(this); content.addView(web, new FrameLayout.LayoutParams(-1, -1));
        errorPanel = new LinearLayout(this); errorPanel.setOrientation(LinearLayout.VERTICAL);
        errorPanel.setGravity(Gravity.CENTER); errorPanel.setPadding(dp(28), dp(24), dp(28), dp(24));
        TextView errorTitle = new TextView(this); errorTitle.setText(R.string.connection_error); errorTitle.setTextSize(20);
        TextView errorText = new TextView(this); errorText.setText(R.string.connection_help); errorText.setTextSize(16);
        errorText.setPadding(0, dp(16), 0, dp(16));
        Button retry = new Button(this); retry.setText(R.string.retry); retry.setOnClickListener(view -> reconnect());
        Button address = new Button(this); address.setText(R.string.server_address); address.setOnClickListener(view -> addressDialog());
        errorPanel.addView(errorTitle); errorPanel.addView(errorText); errorPanel.addView(retry); errorPanel.addView(address);
        errorPanel.setVisibility(View.GONE); content.addView(errorPanel, new FrameLayout.LayoutParams(-1, -1));
        root.addView(content, new LinearLayout.LayoutParams(-1, 0, 1)); setContentView(root);
        configureInsets(); configureWebView();push=new NativePush(this,web,()->serverOrigin);push.intent(getIntent()); colors(); reconnect(); updates=new AppUpdates(this);
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    private void configureInsets() {
        if (Build.VERSION.SDK_INT >= 30) {
            getWindow().setDecorFitsSystemWindows(false);
            root.setOnApplyWindowInsetsListener((view, insets) -> {
                android.graphics.Insets padding = insets.getInsets(WindowInsets.Type.systemBars()
                    | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                view.setPadding(padding.left, padding.top, padding.right, padding.bottom);
                return WindowInsets.CONSUMED;
            });
        } else root.setFitsSystemWindows(true);
    }

    private void colors() {
        boolean dark = (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
        int background = Color.parseColor(dark ? "#141517" : "#f6f6f3");
        root.setBackgroundColor(background); brand.setBackgroundColor(background); errorPanel.setBackgroundColor(background);
        title.setTextColor(dark ? Color.WHITE : Color.parseColor("#1c1d1f")); menu.setTextColor(title.getCurrentTextColor());
        for (int i = 0; i < errorPanel.getChildCount(); i++)
            if (errorPanel.getChildAt(i) instanceof TextView) ((TextView) errorPanel.getChildAt(i)).setTextColor(title.getCurrentTextColor());
        web.setBackgroundColor(background); getWindow().setStatusBarColor(background); getWindow().setNavigationBarColor(background);
        getWindow().getDecorView().setSystemUiVisibility(dark ? 0 : View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
    }

    private void configureWebView() {
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true); settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false); settings.setAllowContentAccess(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setSafeBrowsingEnabled(true); settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setSupportMultipleWindows(false); settings.setBuiltInZoomControls(true); settings.setDisplayZoomControls(false);
        settings.setUserAgentString(settings.getUserAgentString() + " telegent-android/0.1.5");
        CookieManager.getInstance().setAcceptCookie(true); CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);
        WebView.setWebContentsDebuggingEnabled(false);
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();
                String[] pushCommand=PushPolicy.command(url);
                if(pushCommand!=null){if(request.isForMainFrame()&&NavigationPolicy.sameOrigin(view.getUrl(),serverOrigin))push.command(pushCommand);return true;}
                String[] share=NavigationPolicy.shareRequest(url);
                if(share!=null){if(request.isForMainFrame()&&NavigationPolicy.sameOrigin(view.getUrl(),serverOrigin))share(share[0],share[1]);return true;}
                if (!request.isForMainFrame()) return !NavigationPolicy.sameOrigin(url, serverOrigin);
                if (allowedNavigation(url, !request.hasGesture())) return false;
                if (request.hasGesture()) openExternal(url); else connectionError();
                return true;
            }
            @Override public void onPageStarted(WebView view, String url, android.graphics.Bitmap icon) {
                if (!allowedNavigation(url, true)) { view.stopLoading(); connectionError(); return; }
                pageFailed = false; errorPanel.setVisibility(View.GONE); progress.setVisibility(View.VISIBLE);
                discovering = NavigationPolicy.launcher(url, entry) || NavigationPolicy.launcher(url, NavigationPolicy.DEFAULT_ENTRY);
            }
            @Override public void onPageFinished(WebView view, String url) {
                progress.setVisibility(View.GONE); CookieManager.getInstance().flush();
                if (!pageFailed) errorPanel.setVisibility(View.GONE);
                if(push!=null&&NavigationPolicy.sameOrigin(url,serverOrigin))push.sync();
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) connectionError();
            }
            @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (request.isForMainFrame() && response.getStatusCode() >= 400) connectionError();
            }
            @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.cancel(); connectionError();
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public void onProgressChanged(WebView view, int value) { progress.setProgress(value); }
            @Override public void onPermissionRequest(PermissionRequest request) { request.deny(); }
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileSelection != null) fileSelection.onReceiveValue(null);
                fileSelection = callback;
                try {
                    Intent intent = params.createIntent(); intent.addCategory(Intent.CATEGORY_OPENABLE);
                    startActivityForResult(intent, PICK_FILES);
                } catch (ActivityNotFoundException error) {
                    fileSelection.onReceiveValue(null); fileSelection = null; toast(R.string.file_picker_error);
                }
                return true;
            }
        });
        web.setDownloadListener((url, agent, disposition, mime, length) -> download(url, disposition, mime));
    }

    private boolean allowedNavigation(String url, boolean automatic) {
        if (NavigationPolicy.launcher(url, entry) || NavigationPolicy.launcher(url, NavigationPolicy.DEFAULT_ENTRY)) return true;
        if (NavigationPolicy.sameOrigin(url, serverOrigin)) return true;
        if (discovering && automatic && NavigationPolicy.discoveredServer(url)) {
            serverOrigin = NavigationPolicy.origin(url);
            preferences.edit().putString("server_origin", serverOrigin).apply(); return true;
        }
        return false;
    }

    private void reconnect() {
        discovering = true; pageFailed = false; errorPanel.setVisibility(View.GONE);
        web.loadUrl(entry);
    }

    private void connectionError() { pageFailed = true; progress.setVisibility(View.GONE); errorPanel.setVisibility(View.VISIBLE); }

    private void showOptions() {
        PopupMenu popup = new PopupMenu(this, menu);
        popup.getMenu().add(0, 1, 0, R.string.reconnect);
        popup.getMenu().add(0, 2, 1, R.string.server_address);
        popup.getMenu().add(0, 3, 2, R.string.open_browser);
        popup.getMenu().add(0, 4, 3, R.string.about);
        popup.getMenu().add(0, 5, 4, R.string.check_updates);
        popup.getMenu().add(0, 6, 5, R.string.push_notifications).setCheckable(true).setChecked(NativePush.preferences(this).getBoolean("registered",false));
        popup.setOnMenuItemClickListener(item -> {
            if (item.getItemId() == 1) {
                web.evaluateJavascript("if(typeof saveDraft==='function')saveDraft()", value -> reconnect());
            } else if (item.getItemId() == 2) addressDialog();
            else if (item.getItemId() == 3) openExternal(NavigationPolicy.publicUrl(web.getUrl()));
            else if(item.getItemId()==5) updates.check(true);
            else if(item.getItemId()==6) push.toggle();
            else new AlertDialog.Builder(this).setTitle(R.string.about).setMessage(R.string.about_text).setPositiveButton(R.string.ok, null).show();
            return true;
        }); popup.show();
    }

    private void addressDialog() {
        EditText field = new EditText(this); field.setSingleLine(true); field.setText(entry);
        field.setHint(R.string.server_hint); field.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle(R.string.server_address).setView(field)
            .setPositiveButton(R.string.connect, null).setNegativeButton(R.string.cancel, null).create();
        dialog.setOnShowListener(event -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(view -> {
            String next = NavigationPolicy.entry(field.getText().toString());
            if (next == null) { field.setError(getString(R.string.invalid_address)); return; }
            web.evaluateJavascript("if(typeof saveDraft==='function')saveDraft()", value -> {
                entry = next; serverOrigin = NavigationPolicy.launcher(next, NavigationPolicy.DEFAULT_ENTRY) ? null : NavigationPolicy.origin(next);
                preferences.edit().putString("entry", entry).putString("server_origin", serverOrigin).apply();
                dialog.dismiss(); reconnect();
            });
        })); dialog.show();
    }

    private void openExternal(String value) {
        if (value == null) return;
        Uri uri = Uri.parse(value); String scheme = uri.getScheme();
        if (!"https".equalsIgnoreCase(scheme) && !"http".equalsIgnoreCase(scheme)
                && !"mailto".equalsIgnoreCase(scheme) && !"tel".equalsIgnoreCase(scheme)) { toast(R.string.unsupported_link); return; }
        try { startActivity(new Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE)); }
        catch (ActivityNotFoundException error) { toast(R.string.unsupported_link); }
    }

    private void download(String url, String disposition, String mime) {
        download(url,disposition,mime,false);
    }

    private void share(String kind,String id) {
        String origin=serverOrigin;
        String script="typeof nativeSharePayload==='function'?nativeSharePayload('"+kind+"',"+id+"):null";
        web.evaluateJavascript(script,value->{
            if(destroyed||!NavigationPolicy.sameOrigin(web.getUrl(),origin)||!origin.equals(serverOrigin))return;
            try{
                Object decoded=new org.json.JSONTokener(value).nextValue();
                if(decoded instanceof String)decoded=new org.json.JSONObject((String)decoded);
                if(!(decoded instanceof org.json.JSONObject))throw new IllegalArgumentException();
                org.json.JSONObject payload=(org.json.JSONObject)decoded;
                if("message".equals(kind)){
                    String text=payload.getString("text");if(text.isEmpty()||text.length()>200000)throw new IllegalArgumentException();
                    startActivity(Intent.createChooser(new Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT,text),getString(R.string.share)));
                }else{
                    if(!id.equals(payload.getString("id")))throw new IllegalArgumentException();
                    String name=NavigationPolicy.downloadName(null,payload.getString("name"));
                    String type=URLConnection.guessContentTypeFromName(name);
                    download(origin+"/api/attachments/"+id,"attachment; filename*=UTF-8''"+Uri.encode(name),type,true);
                }
            }catch(Exception error){toast(R.string.share_error);}
        });
    }

    private void download(String url, String disposition, String mime,boolean sharing) {
        if (!NavigationPolicy.attachment(url, serverOrigin)) { toast(R.string.download_error); return; }
        if (downloadBusy) { toast(R.string.download_busy); return; }
        String cookie = CookieManager.getInstance().getCookie(url);
        String agent = web.getSettings().getUserAgentString();
        String name = NavigationPolicy.downloadName(disposition, URLUtil.guessFileName(url, disposition, mime));
        String type = mime != null && mime.matches("[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+") ? mime : "application/octet-stream";
        downloadBusy = true; downloadCancelled = false;
        TextView status = new TextView(this); status.setPadding(dp(24), dp(16), dp(24), dp(16)); status.setText(getString(R.string.download_progress, 0));
        downloadDialog = new AlertDialog.Builder(this).setTitle(R.string.download).setView(status)
            .setNegativeButton(R.string.cancel, (dialog, which) -> cancelDownload()).setOnCancelListener(dialog -> cancelDownload()).show();
        files.execute(() -> {
            File cache = null;
            try {
                cache = File.createTempFile("telegent-download-", ".part", getCacheDir());
                HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection(); downloadConnection = connection;
                connection.setInstanceFollowRedirects(false); connection.setConnectTimeout(20000); connection.setReadTimeout(30000);
                connection.setRequestProperty("User-Agent", agent);
                if (cookie != null) connection.setRequestProperty("Cookie", cookie);
                if (connection.getResponseCode() != 200) throw new IOException("Download rejected");
                long expected = connection.getContentLengthLong();
                if (expected > MAX_FILE_BYTES) throw new IOException("File too large");
                if (expected > 0 && getCacheDir().getUsableSpace() < expected + 16L * 1024 * 1024) {
                    main.post(() -> toast(R.string.not_enough_space)); throw new IOException("No cache space");
                }
                long count = 0, report = -1;
                try (InputStream input = connection.getInputStream(); OutputStream output = new FileOutputStream(cache)) {
                    byte[] buffer = new byte[65536]; int size;
                    while ((size = input.read(buffer)) != -1) {
                        if (downloadCancelled || Thread.currentThread().isInterrupted()) throw new IOException("Cancelled");
                        count += size; if (count > MAX_FILE_BYTES) throw new IOException("File too large");
                        output.write(buffer, 0, size);
                        long megabytes = count / 1024 / 1024;
                        if (megabytes != report) { report = megabytes; int value = (int) megabytes; main.post(() -> status.setText(getString(R.string.download_progress, value))); }
                    }
                }
                if (expected >= 0 && count != expected) throw new IOException("Incomplete download");
                File complete = cache; cache = null;
                main.post(() -> {
                    if (destroyed || downloadCancelled) { complete.delete(); finishDownload(); return; }
                    if (downloadDialog != null) downloadDialog.dismiss();
                    if(sharing){
                        try{
                            Uri uri=SharedFilesProvider.prepare(this,complete,name);
                            Intent send=new Intent(Intent.ACTION_SEND).setType(type).putExtra(Intent.EXTRA_STREAM,uri)
                                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                            send.setClipData(ClipData.newUri(getContentResolver(),name,uri));
                            startActivity(Intent.createChooser(send,getString(R.string.share)).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION));
                        }catch(IOException|ActivityNotFoundException error){complete.delete();toast(R.string.share_error);}
                        finishDownload();return;
                    }
                    downloadedFile = complete;
                    Intent save = new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType(type).putExtra(Intent.EXTRA_TITLE, name);
                    try { startActivityForResult(save, SAVE_FILE); }
                    catch (ActivityNotFoundException error) { toast(R.string.save_error); finishDownload(); }
                });
            } catch (IOException error) {
                main.post(() -> { if (!destroyed && !downloadCancelled) toast(R.string.download_error); finishDownload(); });
            } finally {
                if (cache != null) cache.delete();
                if (downloadConnection != null) downloadConnection.disconnect(); downloadConnection = null;
            }
        });
    }

    private void cancelDownload() {
        downloadCancelled = true;
        if (downloadConnection != null) downloadConnection.disconnect();
    }

    private void finishDownload() {
        if (downloadDialog != null) { downloadDialog.dismiss(); downloadDialog = null; }
        if (downloadedFile != null) { downloadedFile.delete(); downloadedFile = null; }
        downloadBusy = false;
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == PICK_FILES && fileSelection != null) {
            Uri[] selected = WebChromeClient.FileChooserParams.parseResult(result, data);
            if (selected != null && selected.length > 30) { toast(R.string.too_many_files); selected = null; }
            fileSelection.onReceiveValue(selected); fileSelection = null;
        } else if (request == SAVE_FILE && downloadedFile != null) {
            if (result != RESULT_OK || data == null || data.getData() == null) { finishDownload(); return; }
            Uri destination = data.getData(); File source = downloadedFile;
            files.execute(() -> {
                boolean saved = false;
                try (InputStream input = new FileInputStream(source); OutputStream output = getContentResolver().openOutputStream(destination, "w")) {
                    if (output == null) throw new IOException("No output stream");
                    byte[] buffer = new byte[65536]; int size;
                    while ((size = input.read(buffer)) != -1) output.write(buffer, 0, size);
                    output.flush(); saved = true;
                } catch (IOException | SecurityException error) { /* A provider may revoke the write grant. */ }
                boolean complete = saved;
                main.post(() -> { if (!destroyed) toast(complete ? R.string.file_saved : R.string.save_error); finishDownload(); });
            });
        }
    }

    @Override public void onBackPressed() {
        if (web == null || !NavigationPolicy.sameOrigin(web.getUrl(), serverOrigin)) { backInPage(); return; }
        String script = "(function(){var d=document.querySelector('dialog[open]');if(d){var e=new Event('cancel',{cancelable:true});if(d.dispatchEvent(e))d.close();return true;}"
            + "if(typeof $!=='function')return false;if($('filters')&&$('filters').classList.contains('open')){$('closeFilters').click();return true;}"
            + "if($('menu')&&!$('menu').classList.contains('hidden')){closeMenu();return true;}"
            + "if(document.querySelector('#projects.open,#aside.open')){closeMobilePanels();return true;}return false;})()";
        web.evaluateJavascript(script, value -> { if (!destroyed && !"true".equals(value)) backInPage(); });
    }

    private void backInPage() { if (web != null && web.canGoBack()) web.goBack(); else moveTaskToBack(true); }
    private void toast(int message) { if (!destroyed) Toast.makeText(this, message, Toast.LENGTH_LONG).show(); }
    @Override protected void onPause() { if(updates!=null)updates.pause(); web.onPause(); web.pauseTimers(); CookieManager.getInstance().flush(); super.onPause(); }
    @Override protected void onResume() { super.onResume(); if (web != null) { web.resumeTimers(); web.onResume(); } if(updates!=null)updates.resume();if(push!=null)push.sync(); }
    @Override protected void onNewIntent(Intent intent){super.onNewIntent(intent);setIntent(intent);if(push!=null)push.intent(intent);}
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] results){super.onRequestPermissionsResult(request,permissions,results);if(request==NativePush.PERMISSION&&push!=null)push.permissionResult();}
    @Override public void onConfigurationChanged(Configuration configuration) { super.onConfigurationChanged(configuration); colors(); }
    @Override protected void onDestroy() {
        destroyed = true; cancelDownload(); files.shutdownNow(); finishDownload();
        if(updates!=null)updates.destroy();
        if (fileSelection != null) { fileSelection.onReceiveValue(null); fileSelection = null; }
        if (web != null) { web.stopLoading(); web.destroy(); }
        super.onDestroy();
    }
}
