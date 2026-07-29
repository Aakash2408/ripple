// frontend/src/api/users.ts
// Consumer of POST /users API

import { httpClient } from '../lib/http';

interface CreateUserRequest {
  name: string;
  email: string;
  age?: number;
}

export async function createUser(data: CreateUserRequest): Promise<User> {
  const response = await httpClient.post('/users', {
    name: data.name,
    email: data.email,
    age: data.age,
  });
  return response.data;
}

export async function listUsers(): Promise<User[]> {
  const response = await httpClient.get('/users');
  return response.data;
}
