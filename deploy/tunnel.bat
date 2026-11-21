@echo off
rem Opens the private tunnel to the IER server. Keep this window open while you use the dashboard.
rem Then browse to http://localhost:3000   (API is http://localhost:8000). Nothing is public.
rem Edit SERVER below if the address changes.
set SERVER=ier@2.29.197.65
echo Tunnel to %SERVER% ... press Ctrl+C to close.
ssh -N -L 3000:localhost:3000 -L 8000:localhost:8000 %SERVER% ^
  "kubectl -n ier port-forward svc/dashboard 3000:3000 & kubectl -n ier port-forward svc/api 8000:8000 & wait"
