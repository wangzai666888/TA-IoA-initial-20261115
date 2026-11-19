FROM node:22-bookworm-slim AS build
WORKDIR /frontend
COPY im_server/frontend/package.json im_server/frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY im_server/frontend/public ./public
COPY im_server/frontend/src ./src
RUN npm run build
FROM nginx:stable-alpine
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /frontend/build /usr/share/nginx/html
EXPOSE 80
