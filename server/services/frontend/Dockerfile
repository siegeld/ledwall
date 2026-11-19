FROM node:22-alpine AS build
WORKDIR /app
COPY services/frontend/package.json ./
RUN npm install --no-audit --no-fund
COPY services/frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY services/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
