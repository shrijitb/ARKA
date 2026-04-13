# Arka — always-on display / web target
#
# This Docker image is for screens that want to display Arka 24/7 via a browser
# (e.g. a TV, a dedicated monitor, a wall display connected to a Pi).
#
# Desktop users: run `npm run electron:build` for a native Electron app.
# Mobile users:  run `npm run cap:ios` or `npm run cap:android`.
#
# The built app calls the hypervisor directly at the URL the user enters
# during first launch (ConnectionGate). No API proxy in nginx.

FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
# Install only prod + build deps (skip Electron/Capacitor heavy binaries)
RUN npm install --ignore-scripts
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
