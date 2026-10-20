package io.github.takymu.telegent;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.widget.Toast;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.json.JSONObject;

/** Download in the foreground lifecycle; installation always uses Android's confirmation. */
final class AppUpdates {
    private final Activity activity;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private boolean busy, destroyed, waitingPermission, foreground;
    private AlertDialog prompt;
    private long offered;
    private File ready;

    AppUpdates(Activity owner) { activity=owner; }

    void resume() {
        foreground=true;
        if (waitingPermission) {
            waitingPermission=false;
            if (activity.getPackageManager().canRequestPackageInstalls()) install();
            return;
        }
        check(false);
    }

    void check(boolean manual) {
        if (destroyed || busy) return;
        if (ready != null && manual) { install(); return; }
        busy=true;
        if (manual) message(R.string.checking_updates);
        worker.execute(() -> {
            File temporary=null;
            try {
                JSONObject release=new JSONObject(new String(fetch(UpdatePolicy.FEED,65536),java.nio.charset.StandardCharsets.UTF_8)).getJSONObject("android");
                long code=release.getLong("version_code"), size=release.getLong("bytes");
                String digest=release.getString("sha256"), url=release.getString("url");
                PackageInfo installed=activity.getPackageManager().getPackageInfo(activity.getPackageName(),0);
                if (code<=installed.versionCode) { if(manual) main.post(()->message(R.string.latest_version)); return; }
                if (!UpdatePolicy.newer(installed.versionCode,code,size,digest,url)) throw new IOException("Invalid release");
                File directory=new File(activity.getFilesDir(),"updates");
                if (!directory.isDirectory()&&!directory.mkdirs()) throw new IOException("No update directory");
                if (directory.getUsableSpace()<size+16L*1024*1024) throw new IOException("No update space");
                File apk=new File(directory,"update.apk");
                if (!apk.isFile() || apk.length()!=size || !digest.equals(hash(apk))) {
                    temporary=new File(directory,"update.part");
                    download(url,temporary,size);
                    if (!digest.equals(hash(temporary))) throw new IOException("Checksum mismatch");
                    verify(temporary,code);
                    if (!temporary.renameTo(apk)) throw new IOException("Could not save update");
                    temporary=null;
                }
                verify(apk,code);
                main.post(() -> {
                    if (destroyed) return;
                    ready=apk; offered=code;
                    offer();
                });
            } catch (Exception error) { main.post(()->{if(manual)message(R.string.update_error);else offer();}); }
            finally {
                if(temporary!=null)temporary.delete();
                main.post(()->busy=false);
            }
        });
    }

    void pause() { foreground=false; }

    private void offer() {
        if(destroyed||!foreground||ready==null||prompt!=null)return;
        prompt=new AlertDialog.Builder(activity).setTitle(R.string.update_ready).setMessage(R.string.update_help)
            .setPositiveButton(R.string.install_update,(dialog,which)->install())
            .setNegativeButton(R.string.later,null).create();
        prompt.setOnDismissListener(dialog->prompt=null);prompt.show();
    }

    private HttpURLConnection connect(String value) throws IOException {
        for(int redirects=0;redirects<6;redirects++) {
            if(!UpdatePolicy.transport(value))throw new IOException("Untrusted release address");
            HttpURLConnection connection=(HttpURLConnection)new URL(value).openConnection();
            connection.setInstanceFollowRedirects(false);connection.setConnectTimeout(15000);connection.setReadTimeout(30000);
            connection.setRequestProperty("User-Agent","telegent-android-updater/1");
            int status=connection.getResponseCode();
            if(status==200)return connection;
            String location=connection.getHeaderField("Location");connection.disconnect();
            if(status<300||status>399||location==null)throw new IOException("Release unavailable");
            value=new URL(new URL(value),location).toString();
        }
        throw new IOException("Too many redirects");
    }
    private byte[] fetch(String value,int limit)throws IOException {
        HttpURLConnection connection=connect(value);
        try(InputStream input=connection.getInputStream();ByteArrayOutputStream output=new ByteArrayOutputStream()) {
            byte[] buffer=new byte[8192];int size;
            while((size=input.read(buffer))!=-1){if(output.size()+size>limit)throw new IOException("Release too large");output.write(buffer,0,size);}
            return output.toByteArray();
        }finally{connection.disconnect();}
    }
    private void download(String url,File file,long expected)throws IOException {
        HttpURLConnection connection=connect(url);
        try(InputStream input=connection.getInputStream();FileOutputStream output=new FileOutputStream(file)) {
            byte[] buffer=new byte[65536];long count=0;int size;
            while((size=input.read(buffer))!=-1){count+=size;if(count>expected||Thread.currentThread().isInterrupted())throw new IOException("Invalid download");output.write(buffer,0,size);}
            if(count!=expected)throw new IOException("Incomplete download");
        }finally{connection.disconnect();}
    }
    private String hash(File file)throws Exception {
        MessageDigest digest=MessageDigest.getInstance("SHA-256");
        try(InputStream input=new FileInputStream(file)){byte[] buffer=new byte[65536];int size;while((size=input.read(buffer))!=-1)digest.update(buffer,0,size);}
        StringBuilder text=new StringBuilder();for(byte value:digest.digest())text.append(String.format(java.util.Locale.ROOT,"%02x",value&255));return text.toString();
    }
    private void verify(File file,long code)throws Exception {
        PackageManager manager=activity.getPackageManager();
        PackageInfo update=manager.getPackageArchiveInfo(file.getAbsolutePath(),PackageManager.GET_SIGNATURES);
        PackageInfo installed=manager.getPackageInfo(activity.getPackageName(),PackageManager.GET_SIGNATURES);
        if(update==null||!installed.packageName.equals(update.packageName)||update.versionCode!=code
                ||code<=installed.versionCode||!sameSignatures(installed.signatures,update.signatures))throw new IOException("Wrong package or signer");
    }
    private boolean sameSignatures(Signature[] first,Signature[] second) {
        if(first==null||second==null||first.length==0||first.length!=second.length)return false;
        for(int i=0;i<first.length;i++)if(!Arrays.equals(first[i].toByteArray(),second[i].toByteArray()))return false;
        return true;
    }
    private void install() {
        if(destroyed||ready==null)return;
        if(!activity.getPackageManager().canRequestPackageInstalls()) {
            waitingPermission=true;
            try{activity.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,Uri.parse("package:"+activity.getPackageName())));}
            catch(Exception error){waitingPermission=false;message(R.string.update_error);}
            return;
        }
        try {
            verify(ready,offered);
            activity.startActivity(new Intent(Intent.ACTION_VIEW)
                .setDataAndType(Uri.parse("content://io.github.takymu.telegent.updates/telegent.apk"),"application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION));
        }catch(Exception error){message(R.string.update_error);}
    }
    private void message(int resource){if(!destroyed)Toast.makeText(activity,resource,Toast.LENGTH_LONG).show();}
    void destroy(){destroyed=true;worker.shutdownNow();if(prompt!=null)prompt.dismiss();}
}
