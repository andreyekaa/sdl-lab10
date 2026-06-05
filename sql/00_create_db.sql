CREATE ROLE app_user LOGIN PASSWORD 'app_pass';
CREATE DATABASE event_hub OWNER app_user;
