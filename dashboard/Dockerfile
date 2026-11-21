FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# The browser calls the API at this address (baked in at build time), so it must be an address
# your browser can reach, for example a Tailscale name or a path behind your login proxy.
ARG NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app ./
USER node
EXPOSE 3000
CMD ["npm", "start"]
