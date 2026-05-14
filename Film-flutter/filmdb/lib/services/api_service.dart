import 'dart:convert';
import 'package:http/http.dart' as http;
import '../models/movie.dart';

class ApiService {
  static const String _baseUrl = 'https://www.omdbapi.com/';
  static const String _apiKey = 'b09e31c7';

  Future<List<Movie>> searchMovies(String query) async {
    final uri = Uri.parse('$_baseUrl?s=$query&apikey=$_apiKey');

    final response = await http.get(uri);

    if (response.statusCode != 200) {
      throw Exception('Errore HTTP nella ricerca film');
    }

    final data = jsonDecode(response.body);

    if (data['Response'] != 'True') {
      return [];
    }

    final List moviesJson = data['Search'];

    return moviesJson.map((movieJson) => Movie.fromJson(movieJson)).toList();
  }

  Future<Movie> getMovieDetails(String imdbId) async {
    final uri = Uri.parse('$_baseUrl?i=$imdbId&plot=full&apikey=$_apiKey');

    final response = await http.get(uri);

    if (response.statusCode != 200) {
      throw Exception('Errore HTTP nel dettaglio film');
    }

    final data = jsonDecode(response.body);

    if (data['Response'] != 'True') {
      throw Exception('Film non trovato');
    }

    return Movie.fromJson(data);
  }
}