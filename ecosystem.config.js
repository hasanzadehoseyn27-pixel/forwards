module.exports = {
  apps: [
    {
      name: "forwardsbot",
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
