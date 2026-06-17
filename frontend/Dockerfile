# Dev-oriented image for the local stack. For Vercel you deploy from git
# directly (no Dockerfile needed); for a production container, switch to a
# multi-stage `next build` + `next start`.
FROM node:20-alpine

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .

EXPOSE 3000
CMD ["npm", "run", "dev"]
