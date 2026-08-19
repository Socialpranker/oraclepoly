# Build stage
FROM node:20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json .npmrc ./
RUN npm install

COPY . .

ARG VITE_SENTRY_DSN
ARG VITE_TELEGRAM_BOT_USERNAME
ARG VITE_BOT_USERNAME

RUN npm run build

# Serve stage
FROM nginx:alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
