class Movie
{
  final String id;
  final String title;
  final String year;
  final String poster;
  final String plot;
  final double rating;

  const Movie
  ({
    required this.id,
    required this.title,
    required this.year,
    required this.poster,
    required this.plot,
    required this.rating,
  });

  factory Movie.fromJson(Map<String, dynamic>json)
  {
    return Movie(
      id: json ['imdbID'] ?? '',
      title: json ['Title'] ?? 'Titolo non disponibile',
      year: json ['Year'] ?? 'Anno non disponibile',
      poster: json ['Poster'] ?? '',
      plot: json ['Plot'] ?? 'Trama non disponibile',
      rating: double.tryParse(json ['imdbRating'] ?? '') ?? 0.0,
    );
  }
}