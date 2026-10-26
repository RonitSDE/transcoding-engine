FROM node:22-alpine AS build

WORKDIR /opt/transcode/services/web

COPY package.json package-lock.json ./
RUN npm ci

COPY index.html tsconfig.json vite.config.ts ./
COPY src ./src
RUN npm run build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /opt/transcode/services/web/dist /usr/share/nginx/html

EXPOSE 80