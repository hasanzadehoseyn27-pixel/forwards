module.exports = {
  apps: [
    {
      // اسم PM2 قابل تغییر: $env:PM2_NAME="k1ads"; pm2 start ecosystem.config.js
      name: process.env.PM2_NAME || "forwardsbot",
      cwd: __dirname,
      script: ".venv/Scripts/python.exe",
      args: "-m bestrobot run",
      interpreter: "none",
      autorestart: true,
      watch: false,
      max_restarts: 20,
      restart_delay: 5000,
      env: {
        PYTHONUNBUFFERED: "1"
      }
    }
  ]
};
