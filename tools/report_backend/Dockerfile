FROM node:24.19.0-alpine
WORKDIR /service
COPY server.cjs .
RUN mkdir /data && chown node:node /data
USER node
EXPOSE 8080
CMD ["node", "server.cjs"]
