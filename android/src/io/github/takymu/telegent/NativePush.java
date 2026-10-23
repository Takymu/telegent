package io.github.takymu.telegent;

import android.Manifest;
import android.app.Activity;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Handler;
import android.webkit.WebView;
import android.widget.Toast;
import com.google.firebase.FirebaseApp;
import com.google.firebase.FirebaseOptions;
import com.google.firebase.messaging.FirebaseMessaging;
import com.google.android.gms.common.ConnectionResult;
import com.google.android.gms.common.GoogleApiAvailabilityLight;
import java.security.SecureRandom;
import java.util.function.Supplier;
import org.json.JSONObject;
import org.json.JSONTokener;

/** An opt-in native receiver; the main account token never leaves the authenticated page. */
final class NativePush {
    static final String CHANNEL="messages";
    static final int PERMISSION=6401;
    private final Activity activity;
    private final WebView web;
    private final Supplier<String> origin;
    private final SharedPreferences prefs;
    private String nonce;
    private boolean interactive;

    NativePush(Activity activity,WebView web,Supplier<String> origin){
        this.activity=activity;this.web=web;this.origin=origin;this.prefs=preferences(activity);channel(activity);
    }

    static SharedPreferences preferences(Context context){return context.getSharedPreferences("push",Context.MODE_PRIVATE);}
    static String randomKey(int size){byte[] bytes=new byte[size];new SecureRandom().nextBytes(bytes);StringBuilder out=new StringBuilder();for(byte b:bytes)out.append(String.format(java.util.Locale.ROOT,"%02x",b&255));return out.toString();}
    static void channel(Context context){
        NotificationChannel channel=new NotificationChannel(CHANNEL,context.getString(R.string.push_messages),NotificationManager.IMPORTANCE_HIGH);
        channel.setLockscreenVisibility(android.app.Notification.VISIBILITY_PRIVATE);
        context.getSystemService(NotificationManager.class).createNotificationChannel(channel);
    }
    static boolean permitted(Context context){return Build.VERSION.SDK_INT<33||context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)==PackageManager.PERMISSION_GRANTED;}

    static void initialize(Context context,JSONObject options)throws Exception{
        FirebaseOptions next=new FirebaseOptions.Builder().setApplicationId(options.getString("app_id"))
            .setApiKey(options.getString("api_key")).setProjectId(options.getString("project_id"))
            .setGcmSenderId(options.getString("sender_id")).build();
        FirebaseApp current=null;
        try{current=FirebaseApp.getInstance();}catch(IllegalStateException ignored){}
        if(current!=null&&!current.getOptions().getApplicationId().equals(next.getApplicationId())){current.delete();current=null;}
        if(current==null)FirebaseApp.initializeApp(context,next);
    }
    static void restore(Context context){
        SharedPreferences p=preferences(context);
        if(p.getBoolean("wanted",false))try{initialize(context,new JSONObject(p.getString("firebase","{}")));}catch(Exception ignored){}
    }

    void toggle(){
        if(prefs.getBoolean("wanted",false)){disable();toast(R.string.push_disabled);return;}
        if(GoogleApiAvailabilityLight.getInstance().isGooglePlayServicesAvailable(activity)!=ConnectionResult.SUCCESS){toast(R.string.push_play_services);return;}
        if(Build.VERSION.SDK_INT>=33&&!permitted(activity)){activity.requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},PERMISSION);return;}
        begin(true);
    }
    void permissionResult(){if(permitted(activity))begin(true);else toast(R.string.push_permission);}
    void disable(){
        nonce=null;prefs.edit().putBoolean("wanted",false).putBoolean("registered",false).putString("job","remove").apply();
        PushRefreshService.schedule(activity);
        try{FirebaseMessaging.getInstance().setAutoInitEnabled(false);}catch(Exception ignored){}
        activity.getSystemService(NotificationManager.class).cancelAll();
    }
    void sync(){
        long pending=prefs.getLong("pending_message",0);
        if(pending>0&&NavigationPolicy.sameOrigin(web.getUrl(),origin.get()))web.evaluateJavascript("window.NATIVE_PUSH_OPEN="+pending+";if(typeof nativePushFlush==='function')nativePushFlush()",null);
        if(prefs.getBoolean("wanted",false)&&permitted(activity))begin(false);
    }
    void begin(boolean userAction){
        String server=origin.get();
        if(nonce!=null||!NavigationPolicy.sameOrigin(web.getUrl(),server))return;
        interactive=userAction;
        nonce=randomKey(16);String expected=nonce;
        new Handler(activity.getMainLooper()).postDelayed(()->{if(expected.equals(nonce)){nonce=null;if(userAction)toast(R.string.push_error);}},60000);
        web.evaluateJavascript("typeof nativePushConfig==='function'?JSON.stringify(nativePushConfig()):null",value->{
            if(!expected.equals(nonce))return;
            if(!NavigationPolicy.sameOrigin(web.getUrl(),server)||!server.equals(origin.get())){nonce=null;return;}
            try{
                Object decoded=new JSONTokener(value).nextValue();if(decoded instanceof String)decoded=new JSONTokener((String)decoded).nextValue();
                if(!(decoded instanceof JSONObject)||!((JSONObject)decoded).optBoolean("configured")){nonce=null;if(userAction)toast(R.string.push_unconfigured);return;}
                JSONObject config=(JSONObject)decoded,options=config.getJSONObject("firebase");
                String serverId=config.getString("server_id");if(!serverId.matches("[0-9a-f]{32}"))throw new IllegalArgumentException();
                initialize(activity,options);
                String key=prefs.getString("device_key",null);if(key==null)key=randomKey(32);
                boolean sameServer=serverId.equals(prefs.getString("server_id",null));
                prefs.edit().putString("device_key",key).putString("firebase",options.toString()).putString("endpoint",server)
                    .putBoolean("registered",sameServer&&prefs.getBoolean("registered",false))
                    .putString("server_id",serverId).putBoolean("wanted",true).putString("binding",expected).putString("job","refresh").apply();
                String deviceKey=key;
                FirebaseMessaging.getInstance().setAutoInitEnabled(true);
                FirebaseMessaging.getInstance().getToken().addOnCompleteListener(activity,task->{
                    if(!expected.equals(nonce))return;
                    if(!task.isSuccessful()||!NavigationPolicy.sameOrigin(web.getUrl(),server)||!server.equals(origin.get())){nonce=null;if(userAction)toast(R.string.push_error);return;}
                    prefs.edit().putString("fcm_token",task.getResult()).apply();
                    web.evaluateJavascript("nativePushEnroll("+JSONObject.quote(task.getResult())+","+JSONObject.quote(deviceKey)+","+JSONObject.quote(expected)+")",null);
                });
            }catch(Exception error){nonce=null;if(userAction)toast(R.string.push_error);}
        });
    }
    void command(String[] parts){
        if("toggle".equals(parts[0]))toggle();
        else if("sync".equals(parts[0]))sync();
        else if("logout".equals(parts[0]))disable();
        else if("opened".equals(parts[0])){if(prefs.getLong("pending_message",0)==PushPolicy.messageId(parts[1]))prefs.edit().remove("pending_message").apply();}
        else if("result".equals(parts[0])&&parts[1].equals(nonce)){
            boolean ok="ok".equals(parts[2]);nonce=null;prefs.edit().putBoolean("registered",ok).apply();
            if(interactive)toast(ok?R.string.push_enabled:R.string.push_error);
        }
    }
    void intent(Intent intent){
        if(intent!=null&&prefs.getBoolean("registered",false)&&PushPolicy.accepts(prefs.getString("server_id",null),intent.getStringExtra("server_id"),intent.getStringExtra("message_id"))){
            prefs.edit().putLong("pending_message",PushPolicy.messageId(intent.getStringExtra("message_id"))).apply();
            sync();
        }
    }
    private void toast(int id){Toast.makeText(activity,id,Toast.LENGTH_LONG).show();}
}
